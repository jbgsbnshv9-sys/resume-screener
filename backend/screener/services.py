"""Operations shared by the CLI and the API. Every state change writes the audit log.

The rules that keep review meaningful live here, not in the UI:
- a decision needs a recorded `opened` event by the same reviewer, and a reason;
- decisions are one application at a time (there is no bulk entry point);
- a requisition's config is pinned at its first scored candidate;
- promotion needs a passing eval on the exact config hash (or an admin override
  with a justification) and acknowledged lint findings;
- an opted-out candidate is never run through the pipeline, and any earlier AI
  output for them is hidden from reviewers.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from screener import audit, blobs, settings
from screener.config import TIER_ORDER, LoadedConfig, load_config_text
from screener.evals.runner import EvalReport
from screener.hashing import hash_value
from screener.lint.rubric_lint import unacknowledged
from screener.llm import ClientProvider
from screener.models.db import (
    Application,
    AuditLog,
    Candidate,
    CandidateNotice,
    ConsentRecord,
    CriterionScoreRow,
    Decision,
    Document,
    EvalRunRow,
    FlagRow,
    JobRole,
    NodeOutputRow,
    PipelineConfigRow,
    RedactionMapRow,
    Requisition,
    ReviewerAssignment,
    ReviewEvent,
    Run,
    User,
    utcnow,
)
from screener.pipeline.runner import RunResult, run_pipeline
from screener.store import ensure_config, save_run


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# --- identity ---------------------------------------------------------------


def person_key(email: str | None, fallback: str) -> str:
    """Keyed hash for dedup, so the candidates table never holds the raw email."""
    secret = os.environ.get("SCREENER_PERSON_KEY_SECRET", "dev-only-secret").encode()
    value = (email or "").strip().lower() or f"doc:{fallback}"
    return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_user(session: Session, email: str, display_name: str, role: str, actor: str) -> tuple[User, str]:
    token = os.urandom(24).hex()
    user = User(email=email, display_name=display_name, role=role, api_token_hash=hash_token(token))
    session.add(user)
    session.flush()
    audit.record(session, actor, "user.create", "user", user.id, detail={"role": role})
    return user, token


def user_for_token(session: Session, token: str) -> User | None:
    return session.scalars(select(User).where(User.api_token_hash == hash_token(token), User.active.is_(True))).first()


def retention_days(jurisdiction: str) -> int:
    path = settings.configs_dir() / "retention.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    return int((data.get("jurisdictions") or {}).get(jurisdiction, data.get("default_days", 365)))


# --- configs ----------------------------------------------------------------


def register_config(session: Session, yaml_text: str, actor: str) -> tuple[PipelineConfigRow, LoadedConfig]:
    loaded = load_config_text(yaml_text)
    return ensure_config(session, loaded, actor), loaded


def loaded_for(row: PipelineConfigRow) -> LoadedConfig:
    """Re-resolve a stored config and refuse if rubric, prompts or term lists changed since it was hashed."""
    loaded = load_config_text(row.yaml_text)
    if loaded.hash != row.hash:
        raise ServiceError(409, f"config {row.hash[:12]} no longer resolves to the same hash (rubric, prompt or term list changed); register a new config version")
    return loaded


def store_eval_run(session: Session, report: EvalReport, actor: str) -> EvalRunRow:
    if report.gate is None:
        raise ServiceError(400, "eval report has no gate result")
    row = EvalRunRow(
        config_hash=report.config_hash,
        eval_set_id=report.eval_set_id,
        metrics={k: v.model_dump() for k, v in report.metrics.items()},
        gate=report.gate.model_dump(),
        passed=report.gate.passed,
        report=report.model_dump(mode="json"),
        created_by=actor,
    )
    session.add(row)
    session.flush()
    audit.record(session, actor, "eval.store", "eval_run", row.id, detail={"config_hash": report.config_hash, "passed": report.gate.passed})
    config = session.get(PipelineConfigRow, report.config_hash)
    if config is not None and report.gate.passed and config.status == "draft":
        config.status = "validated"
        audit.record(session, actor, "config.validate", "pipeline_config", config.hash, detail={"eval_run_id": row.id})
    return row


def promote_config(session: Session, config_hash: str, actor: User, *, override_justification: str | None = None) -> PipelineConfigRow:
    if actor.role != "admin":
        raise ServiceError(403, "only an admin can promote a config")
    row = session.get(PipelineConfigRow, config_hash)
    if row is None:
        raise ServiceError(404, "config not found")
    if row.status == "retired":
        raise ServiceError(409, "retired configs can't be promoted")
    loaded = loaded_for(row)
    pending = unacknowledged(loaded.rubric)
    if pending:
        raise ServiceError(409, f"rubric has {len(pending)} unacknowledged lint finding(s): " + "; ".join(f"{f.criterion}.{f.field}: {f.rule}" for f in pending[:5]))
    passing = session.scalars(select(EvalRunRow).where(EvalRunRow.config_hash == config_hash, EvalRunRow.passed.is_(True)).order_by(EvalRunRow.created_at.desc())).first()
    if passing is None:
        if not (override_justification and override_justification.strip()):
            raise ServiceError(409, "no passing eval run for this exact config hash; run the eval suite or record an override justification")
        row.override_justification = override_justification.strip()
    else:
        row.eval_run_id = passing.id
    for other in session.scalars(select(PipelineConfigRow).where(PipelineConfigRow.role_id == row.role_id, PipelineConfigRow.status == "active", PipelineConfigRow.hash != config_hash)):
        other.status = "retired"
        audit.record(session, actor.email, "config.retire", "pipeline_config", other.hash)
    row.status = "active"
    row.promoted_by = actor.email
    row.promoted_at = utcnow()
    role = session.get(JobRole, row.role_id)
    assert role is not None
    role.active_config_hash = row.hash
    audit.record(session, actor.email, "config.promote", "pipeline_config", row.hash, detail={"eval_run_id": row.eval_run_id, "override": bool(row.override_justification), "justification": row.override_justification})
    session.flush()
    return row


# --- requisitions and applications -----------------------------------------


def create_requisition(session: Session, role_id: str, opening_ref: str, actor: str) -> Requisition:
    if session.get(JobRole, role_id) is None:
        raise ServiceError(404, f"role {role_id} not found")
    req = Requisition(role_id=role_id, opening_ref=opening_ref)
    session.add(req)
    session.flush()
    audit.record(session, actor, "requisition.create", "requisition", req.id, detail={"role": role_id, "opening": opening_ref})
    return req


def _config_for(session: Session, req: Requisition) -> PipelineConfigRow:
    if req.pinned_config_hash:
        row = session.get(PipelineConfigRow, req.pinned_config_hash)
        assert row is not None
        return row
    role = session.get(JobRole, req.role_id)
    if role is None or role.active_config_hash is None:
        raise ServiceError(409, "role has no active config; promote one first")
    row = session.get(PipelineConfigRow, role.active_config_hash)
    assert row is not None
    return row


def _store_document(session: Session, application_id: str, data: bytes, filename: str, mime: str, sha: str, render_pdf: bytes | None) -> Document:
    doc = Document(application_id=application_id, filename=filename, blob_ref=blobs.put(data), render_blob_ref=blobs.put(render_pdf) if render_pdf else None, mime=mime, sha256=sha)
    session.add(doc)
    session.flush()
    return doc


def submit_application(
    session: Session,
    *,
    requisition_id: str,
    data: bytes,
    filename: str,
    actor: str,
    provider: ClientProvider,
    jurisdiction: str = "unknown",
    source: str = "upload",
    ai_opt_out: bool = False,
    notice_version: str | None = None,
) -> tuple[Application, Run | None]:
    req = session.get(Requisition, requisition_id)
    if req is None:
        raise ServiceError(404, "requisition not found")
    if req.status != "open":
        raise ServiceError(409, "requisition is closed")
    now = utcnow()
    sha = hashlib.sha256(data).hexdigest()

    if ai_opt_out:
        # Opt-out: never run the pipeline. Store the document for a fully manual review.
        cand = Candidate(person_key=person_key(None, sha), source=source, jurisdiction=jurisdiction, retention_until=now + timedelta(days=retention_days(jurisdiction)))
        session.add(cand)
        session.flush()
        app = Application(candidate_id=cand.id, requisition_id=req.id, status="manual_only", ai_opt_out=True)
        session.add(app)
        session.flush()
        _store_document(session, app.id, data, filename, "application/octet-stream", sha, None)
        session.add(ConsentRecord(application_id=app.id, type="opt_out", recorded_by=actor))
        audit.record(session, actor, "application.submit", "application", app.id, detail={"opt_out": True})
        return app, None

    config_row = _config_for(session, req)
    loaded = loaded_for(config_row)
    run, artifacts = run_pipeline(data, filename, loaded, provider)

    email = run.parse.record.contact.email.text if run.parse and run.parse.record.contact.email else None
    key = person_key(email, sha)
    cand = session.scalars(select(Candidate).where(Candidate.person_key == key)).first()
    if cand is None:
        cand = Candidate(person_key=key, source=source, jurisdiction=jurisdiction, retention_until=now + timedelta(days=retention_days(jurisdiction)))
        session.add(cand)
        session.flush()
    app = session.scalars(select(Application).where(Application.candidate_id == cand.id, Application.requisition_id == req.id)).first()
    previous = None
    if app is None:
        app = Application(candidate_id=cand.id, requisition_id=req.id)
        session.add(app)
        session.flush()
    else:
        previous = session.scalars(select(Run).where(Run.application_id == app.id).order_by(Run.started_at.desc())).first()
        audit.record(session, actor, "application.resubmit", "application", app.id)

    if req.pinned_config_hash is None:
        req.pinned_config_hash = config_row.hash
        req.pinned_at = now
        audit.record(session, actor, "requisition.pin", "requisition", req.id, detail={"config_hash": config_row.hash})
    if notice_version:
        session.add(CandidateNotice(application_id=app.id, notice_version=notice_version, channel=source))

    doc = _store_document(session, app.id, data, filename, run.intake.mime, sha, artifacts.render_pdf)
    row = save_run(session, run, artifacts, application_id=app.id, document_id=doc.id, supersedes=previous.id if previous else None)
    audit.record(session, actor, "run.create", "run", row.id, after={"tier": row.tier, "status": row.status}, detail={"application_id": app.id, "config_hash": config_row.hash})
    return app, row


def record_opt_out(session: Session, application_id: str, actor: str) -> Application:
    app = session.get(Application, application_id)
    if app is None:
        raise ServiceError(404, "application not found")
    app.ai_opt_out = True
    if app.status != "decided":
        app.status = "manual_only"
    session.add(ConsentRecord(application_id=app.id, type="opt_out", recorded_by=actor))
    audit.record(session, actor, "application.opt_out", "application", app.id)
    return app


def rescore_requisition(session: Session, requisition_id: str, config_hash: str, actor: User, provider: ClientProvider) -> list[Run]:
    """Move a requisition to a new config: every open candidate is re-scored (principle 5)."""
    if actor.role != "admin":
        raise ServiceError(403, "only an admin can re-pin a requisition")
    req = session.get(Requisition, requisition_id)
    row = session.get(PipelineConfigRow, config_hash)
    if req is None or row is None:
        raise ServiceError(404, "requisition or config not found")
    if row.status != "active":
        raise ServiceError(409, "a requisition can only move to an active (promoted) config")
    loaded = loaded_for(row)
    runs = []
    for app in session.scalars(select(Application).where(Application.requisition_id == req.id, Application.ai_opt_out.is_(False), Application.status != "decided")):
        doc = session.scalars(select(Document).where(Document.application_id == app.id).order_by(Document.created_at.desc())).first()
        if doc is None:
            continue
        previous = session.scalars(select(Run).where(Run.application_id == app.id).order_by(Run.started_at.desc())).first()
        result, artifacts = run_pipeline(blobs.get(doc.blob_ref), doc.filename, loaded, provider)
        runs.append(save_run(session, result, artifacts, application_id=app.id, document_id=doc.id, supersedes=previous.id if previous else None))
    old = req.pinned_config_hash
    req.pinned_config_hash = row.hash
    req.pinned_at = utcnow()
    audit.record(session, actor.email, "requisition.repin", "requisition", req.id, detail={"from": old, "to": row.hash, "rescored": len(runs)})
    return runs


# --- review -----------------------------------------------------------------


def assign_reviewer(session: Session, reviewer_id: str, requisition_id: str, actor: str) -> ReviewerAssignment:
    user = session.get(User, reviewer_id)
    if user is None or user.role != "reviewer":
        raise ServiceError(400, "assignee must be a reviewer")
    a = ReviewerAssignment(reviewer_id=reviewer_id, requisition_id=requisition_id)
    session.add(a)
    session.flush()
    audit.record(session, actor, "reviewer.assign", "requisition", requisition_id, detail={"reviewer_id": reviewer_id})
    return a


def is_assigned(session: Session, user: User, requisition_id: str) -> bool:
    return session.scalars(select(ReviewerAssignment).where(ReviewerAssignment.reviewer_id == user.id, ReviewerAssignment.requisition_id == requisition_id)).first() is not None


def _require_reviewer_for(session: Session, user: User, app: Application) -> None:
    if user.role != "reviewer" or not is_assigned(session, user, app.requisition_id):
        raise ServiceError(403, "only a reviewer assigned to this requisition can do this")


def record_review_event(session: Session, application_id: str, user: User, event: str, detail: dict[str, Any] | None = None) -> ReviewEvent:
    app = session.get(Application, application_id)
    if app is None:
        raise ServiceError(404, "application not found")
    _require_reviewer_for(session, user, app)
    if event not in ("opened", "viewed_page", "scores_revealed", "closed"):
        raise ServiceError(400, "unknown review event")
    ev = ReviewEvent(application_id=app.id, reviewer_id=user.id, event=event, detail=detail or {})
    session.add(ev)
    if event == "opened" and app.status == "pending":
        app.status = "in_review"
    session.flush()
    return ev


def _events(session: Session, application_id: str, reviewer_id: str | None = None) -> list[ReviewEvent]:
    q = select(ReviewEvent).where(ReviewEvent.application_id == application_id)
    if reviewer_id:
        q = q.where(ReviewEvent.reviewer_id == reviewer_id)
    return list(session.scalars(q.order_by(ReviewEvent.at)))


def latest_run(session: Session, application_id: str) -> Run | None:
    return session.scalars(select(Run).where(Run.application_id == application_id).order_by(Run.started_at.desc())).first()


def record_decision(session: Session, application_id: str, user: User, decision: str, reason: str, override: bool) -> Decision:
    app = session.get(Application, application_id)
    if app is None:
        raise ServiceError(404, "application not found")
    _require_reviewer_for(session, user, app)
    if decision not in ("advance", "hold", "decline"):
        raise ServiceError(400, "decision must be advance, hold, or decline")
    if not reason or not reason.strip():
        raise ServiceError(400, "a reason is required")
    if not any(e.event == "opened" for e in _events(session, app.id, user.id)):
        raise ServiceError(409, "open the candidate's document before recording a decision")
    run = None if app.ai_opt_out else latest_run(session, app.id)
    d = Decision(application_id=app.id, run_id=run.id if run else None, reviewer_id=user.id, decision=decision, reason=reason.strip(), suggested_tier=run.tier if run else None, override=override)
    session.add(d)
    app.status = "in_review" if decision == "hold" else "decided"
    session.flush()
    audit.record(session, user.email, "decision.record", "application", app.id, after={"decision": decision, "override": override, "suggested_tier": d.suggested_tier}, detail={"decision_id": d.id})
    return d


def _ai_visible(session: Session, app: Application, user: User, blind: bool) -> bool:
    if app.ai_opt_out:
        return False
    if blind and user.role == "reviewer":
        return any(e.event == "scores_revealed" for e in _events(session, app.id, user.id))
    return True


def _review_config(session: Session, req: Requisition):
    row = session.get(PipelineConfigRow, req.pinned_config_hash) if req.pinned_config_hash else None
    if row is None:
        from screener.config import ReviewConfig

        return ReviewConfig()
    return load_config_text(row.yaml_text).config.review


def review_queue(session: Session, requisition_id: str, user: User) -> list[dict[str, Any]]:
    req = session.get(Requisition, requisition_id)
    if req is None:
        raise ServiceError(404, "requisition not found")
    if user.role == "reviewer" and not is_assigned(session, user, req.id):
        raise ServiceError(403, "not assigned to this requisition")
    rcfg = _review_config(session, req)
    items = []
    for app in session.scalars(select(Application).where(Application.requisition_id == req.id)):
        run = latest_run(session, app.id)
        visible = _ai_visible(session, app, user, rcfg.blind_first_look)
        flags = [f.type for f in session.scalars(select(FlagRow).where(FlagRow.run_id == run.id))] if run else []
        opened = any(e.event == "opened" for e in _events(session, app.id))
        due = _aware(app.applied_at) + timedelta(days=rcfg.open_sla_days)
        items.append(
            {
                "application_id": app.id,
                "status": app.status,
                "ai_opt_out": app.ai_opt_out,
                "run_id": run.id if run else None,
                "run_status": run.status if run else None,
                "tier": run.tier if (run and visible) else None,
                "tier_hidden": bool(run and run.tier and not visible),
                "needs_attention": bool(run is None or run.needs_attention),
                "flags": sorted(set(flags)),
                "opened": opened,
                "applied_at": app.applied_at.isoformat(),
                "open_by": due.isoformat(),
                "overdue": not opened and utcnow() > due,
            }
        )
    # Undecided first; candidates with no visible tier (unscored, opted out, blind) lead so they're never
    # stranded at the bottom; then strong, review, weak; flagged before unflagged; oldest first.
    rank = {t: i + 1 for i, t in enumerate(reversed(TIER_ORDER))}
    items.sort(key=lambda it: (it["status"] == "decided", rank.get(it["tier"], 0), not it["needs_attention"], it["applied_at"]))
    return items


def alerts(session: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    """Candidates not opened within the SLA, and holds older than the hold limit."""
    now = now or utcnow()
    out = []
    for req in session.scalars(select(Requisition).where(Requisition.status == "open")):
        rcfg = _review_config(session, req)
        for app in session.scalars(select(Application).where(Application.requisition_id == req.id, Application.status != "decided")):
            opened = any(e.event == "opened" for e in _events(session, app.id))
            if not opened and now > _aware(app.applied_at) + timedelta(days=rcfg.open_sla_days):
                out.append({"type": "not_opened", "application_id": app.id, "requisition_id": req.id, "since": app.applied_at.isoformat()})
            last = session.scalars(select(Decision).where(Decision.application_id == app.id).order_by(Decision.at.desc())).first()
            if last is not None and last.decision == "hold" and now > _aware(last.at) + timedelta(days=rcfg.hold_alert_days):
                out.append({"type": "stale_hold", "application_id": app.id, "requisition_id": req.id, "since": last.at.isoformat()})
    return out


def reviewer_metrics(session: Session) -> list[dict[str, Any]]:
    """Override rate and time-to-decision per reviewer. A near-zero override rate is a rubber-stamping signal."""
    out = []
    for user in session.scalars(select(User).where(User.role == "reviewer")):
        decisions = list(session.scalars(select(Decision).where(Decision.reviewer_id == user.id)))
        if not decisions:
            continue
        durations = []
        for d in decisions:
            opened = [e for e in _events(session, d.application_id, user.id) if e.event == "opened" and _aware(e.at) <= _aware(d.at)]
            if opened:
                durations.append((_aware(d.at) - _aware(opened[0].at)).total_seconds())
        with_tier = [d for d in decisions if d.suggested_tier]
        agree = [d for d in with_tier if (d.decision == "advance") == (d.suggested_tier == "strong")]
        durations.sort()
        out.append(
            {
                "reviewer_id": user.id,
                "reviewer": user.email,
                "decisions": len(decisions),
                "override_rate": sum(d.override for d in decisions) / len(decisions),
                "agreement_with_suggested_tier": len(agree) / len(with_tier) if with_tier else None,
                "median_seconds_open_to_decision": durations[len(durations) // 2] if durations else None,
            }
        )
    return out


# --- trace and export -------------------------------------------------------


def run_trace(session: Session, run_id: str, user: User) -> dict[str, Any]:
    run = session.get(Run, run_id)
    if run is None:
        raise ServiceError(404, "run not found")
    app = session.get(Application, run.application_id) if run.application_id else None
    if user.role == "reviewer":
        if app is None or not is_assigned(session, user, app.requisition_id):
            raise ServiceError(403, "not assigned to this requisition")
    elif user.role not in ("admin", "auditor"):
        raise ServiceError(403, "role can't view candidate traces")
    from screener.pipeline.runner import evidence_locations

    result = RunResult.model_validate(run.result)
    hide_ai = bool(app and app.ai_opt_out)
    nodes = list(session.scalars(select(NodeOutputRow).where(NodeOutputRow.run_id == run.id).order_by(NodeOutputRow.seq)))
    return {
        "run_id": run.id,
        "application_id": run.application_id,
        "document_id": run.document_id,
        "config_hash": run.config_hash,
        "status": run.status,
        "stop_reason": run.stop_reason,
        "ai_hidden": hide_ai,
        "tier": None if hide_ai else run.tier,
        "weighted_score": None if hide_ai else run.weighted_score,
        "aggregate": None if hide_ai or result.aggregate is None else result.aggregate.model_dump(mode="json"),
        "flags": [f.model_dump(mode="json") for f in result.flags],
        "hidden_spans": [h.model_dump(mode="json") for h in result.intake.hidden_spans],
        "visible_text": result.intake.visible_text,
        "evidence": [] if hide_ai else [e.model_dump(mode="json") for e in evidence_locations(result)],
        "scores": [] if hide_ai else [s.model_dump(mode="json") for s in result.scores],
        "nodes": [
            {
                "node": n.node,
                "input_hash": n.input_hash,
                "config": n.config,
                "output": n.output if not (hide_ai and n.node in ("score", "aggregate")) else {"hidden": "candidate opted out of AI screening"},
                "models": n.models,
                "prompt_hashes": n.prompt_hashes,
                "input_tokens": n.input_tokens,
                "output_tokens": n.output_tokens,
                "latency_ms": n.latency_ms,
                "llm_calls": len(n.llm_calls),
            }
            for n in nodes
        ],
    }


def redaction_map(session: Session, run_id: str, user: User) -> list[Any]:
    run = session.get(Run, run_id)
    if run is None:
        raise ServiceError(404, "run not found")
    app = session.get(Application, run.application_id) if run.application_id else None
    if user.role == "reviewer":
        if app is None or not is_assigned(session, user, app.requisition_id):
            raise ServiceError(403, "not assigned to this requisition")
    elif user.role not in ("admin", "auditor"):
        raise ServiceError(403, "redaction maps are restricted")
    row = session.get(RedactionMapRow, run_id)
    audit.record(session, user.email, "redaction_map.read", "run", run_id)
    return row.redactions if row else []


def export_application(session: Session, application_id: str, user: User) -> dict[str, Any]:
    if user.role not in ("admin", "auditor"):
        raise ServiceError(403, "exports are for admins and auditors")
    app = session.get(Application, application_id)
    if app is None:
        raise ServiceError(404, "application not found")
    req = session.get(Requisition, app.requisition_id)
    runs = list(session.scalars(select(Run).where(Run.application_id == app.id).order_by(Run.started_at)))
    configs = {r.config_hash: session.get(PipelineConfigRow, r.config_hash) for r in runs}
    events = _events(session, app.id)
    decisions = list(session.scalars(select(Decision).where(Decision.application_id == app.id).order_by(Decision.at)))
    consents = list(session.scalars(select(ConsentRecord).where(ConsentRecord.application_id == app.id)))
    notices = list(session.scalars(select(CandidateNotice).where(CandidateNotice.application_id == app.id)))
    entity_ids = {app.id, *(r.id for r in runs)}
    audit_rows = list(session.scalars(select(AuditLog).where(AuditLog.entity_id.in_(entity_ids)).order_by(AuditLog.id)))
    timeline = sorted(
        [{"at": app.applied_at.isoformat(), "what": "applied"}]
        + [{"at": r.started_at.isoformat(), "what": f"run {r.id} ({r.status}, tier {r.tier})"} for r in runs]
        + [{"at": e.at.isoformat(), "what": f"review event {e.event} by {e.reviewer_id}"} for e in events]
        + [{"at": d.at.isoformat(), "what": f"decision {d.decision} by {d.reviewer_id} (override={d.override})"} for d in decisions],
        key=lambda x: x["at"],
    )
    bundle = {
        "application": {"id": app.id, "requisition_id": app.requisition_id, "status": app.status, "ai_opt_out": app.ai_opt_out, "applied_at": app.applied_at.isoformat()},
        "requisition": {"id": req.id, "role_id": req.role_id, "opening_ref": req.opening_ref, "pinned_config_hash": req.pinned_config_hash} if req else None,
        "configs": {h: {"yaml": c.yaml_text, "rubric_ref": c.rubric_ref, "status": c.status, "eval_run_id": c.eval_run_id, "override_justification": c.override_justification} for h, c in configs.items() if c},
        "runs": [{"id": r.id, "config_hash": r.config_hash, "status": r.status, "tier": r.tier, "supersedes": r.supersedes_run_id, "result": r.result, "redaction_map": (session.get(RedactionMapRow, r.id).redactions if session.get(RedactionMapRow, r.id) else [])} for r in runs],
        "review_events": [{"event": e.event, "reviewer_id": e.reviewer_id, "at": e.at.isoformat(), "detail": e.detail} for e in events],
        "decisions": [{"decision": d.decision, "reason": d.reason, "override": d.override, "suggested_tier": d.suggested_tier, "reviewer_id": d.reviewer_id, "run_id": d.run_id, "at": d.at.isoformat()} for d in decisions],
        "consents": [{"type": c.type, "at": c.recorded_at.isoformat(), "by": c.recorded_by} for c in consents],
        "notices": [{"version": n.notice_version, "channel": n.channel, "at": n.delivered_at.isoformat()} for n in notices],
        "audit": [{"id": a.id, "actor": a.actor, "action": a.action, "entity": f"{a.entity_type}:{a.entity_id}", "at": a.at.isoformat(), "detail": a.detail} for a in audit_rows],
        "timeline": timeline,
    }
    bundle["bundle_hash"] = hash_value(bundle)
    audit.record(session, user.email, "export.application", "application", app.id, detail={"bundle_hash": bundle["bundle_hash"]})
    return bundle


# --- retention ---------------------------------------------------------------


def delete_candidate(session: Session, candidate_id: str, actor: str, reason: str) -> dict[str, int]:
    """Delete a candidate's data. Leaves an audit record of the deletion itself (counts only, no content)."""
    cand = session.get(Candidate, candidate_id)
    if cand is None:
        raise ServiceError(404, "candidate not found")
    counts: dict[str, int] = {}
    apps = list(session.scalars(select(Application).where(Application.candidate_id == cand.id)))
    for app in apps:
        runs = list(session.scalars(select(Run).where(Run.application_id == app.id)))
        for r in runs:
            for model in (NodeOutputRow, CriterionScoreRow, FlagRow):
                for row in session.scalars(select(model).where(model.run_id == r.id)):
                    session.delete(row)
                    counts[model.__tablename__] = counts.get(model.__tablename__, 0) + 1
            rm = session.get(RedactionMapRow, r.id)
            if rm:
                session.delete(rm)
        for d in session.scalars(select(Decision).where(Decision.application_id == app.id)):
            session.delete(d)
            counts["decisions"] = counts.get("decisions", 0) + 1
        session.flush()
        # Runs reference each other (supersedes); clear before deleting.
        for r in runs:
            r.supersedes_run_id = None
        session.flush()
        for r in runs:
            session.delete(r)
        counts["runs"] = counts.get("runs", 0) + len(runs)
        session.flush()
        for model in (ReviewEvent, ConsentRecord, CandidateNotice):
            for row in session.scalars(select(model).where(model.application_id == app.id)):
                session.delete(row)
        for doc in session.scalars(select(Document).where(Document.application_id == app.id)):
            for ref in (doc.blob_ref, doc.render_blob_ref):
                if ref:
                    blobs.delete(ref)
            session.delete(doc)
            counts["documents"] = counts.get("documents", 0) + 1
        session.flush()
        session.delete(app)
    session.flush()
    session.delete(cand)
    session.flush()
    audit.record(session, actor, "candidate.delete", "candidate", candidate_id, detail={"reason": reason, "counts": counts, "applications": len(apps)})
    return counts


def purge_expired(session: Session, actor: str = "system:retention", now: datetime | None = None) -> list[str]:
    now = now or utcnow()
    expired = [c.id for c in session.scalars(select(Candidate).where(Candidate.retention_until.is_not(None), Candidate.retention_until < now))]
    for cid in expired:
        delete_candidate(session, cid, actor, "retention period ended")
    return expired
