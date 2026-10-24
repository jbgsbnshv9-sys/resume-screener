"""HTTP API. Every route authenticates and enforces role-based access.

Roles (SPEC.md, Users and roles):
- admin: users, roles, requisitions, promotion, deletion;
- rubric_owner: configs and evals (synthetic data only, so no candidate traces);
- reviewer: traces and decisions for assigned requisitions;
- auditor: read-only everything, plus exports.
"""

from __future__ import annotations

import csv
import io
import logging
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from screener import audit, blobs, services, settings
from screener.evals.diff import diff_reports, format_diff
from screener.evals.evalset import load_eval_set
from screener.evals.gate import evaluate_gate, load_gate
from screener.evals.runner import EvalReport, report_summary, run_eval
from screener.lint.rubric_lint import lint_rubric
from screener.llm import default_provider
from screener.models.db import Application, AuditLog, Decision, Document, EvalRunRow, JobRole, PipelineConfigRow, Requisition, Run, User
from screener.models.session import get_engine
from screener.services import ServiceError

log = logging.getLogger("screener.api")
EVAL_SETS_DIR = settings.REPO_ROOT / "fixtures" / "evalsets"
GATE_PATH = settings.configs_dir() / "evals" / "default-gate.yaml"
FRONTEND_DIST = settings.REPO_ROOT / "frontend" / "dist"


# Request bodies.


class UserIn(BaseModel):
    email: str
    display_name: str
    role: str



class RoleIn(BaseModel):
    id: str
    title: str
    job_description: str = ""



class ConfigIn(BaseModel):
    yaml: str



class EvalIn(BaseModel):
    eval_set: str
    repeats: int = Field(2, ge=1, le=5)



class PromoteIn(BaseModel):
    override_justification: str | None = None



class RequisitionIn(BaseModel):
    role_id: str
    opening_ref: str



class AssignIn(BaseModel):
    reviewer_id: str



class RescoreIn(BaseModel):
    config_hash: str



class EventIn(BaseModel):
    event: str
    detail: dict[str, Any] = Field(default_factory=dict)



class DecisionIn(BaseModel):
    decision: str
    reason: str
    override: bool



class DeleteIn(BaseModel):
    reason: str



def create_app(session_maker: sessionmaker[Session] | None = None, provider=None, run_evals_inline: bool = False) -> FastAPI:
    app = FastAPI(title="Resume screener", version="0.1.0")
    app.state.session_maker = session_maker or sessionmaker(bind=get_engine(), expire_on_commit=False)
    app.state.provider = provider or default_provider(live=settings.live_llm_enabled())
    app.state.run_evals_inline = run_evals_inline
    app.state.running_evals: set[str] = set()
    app.state.lock = threading.Lock()

    @app.exception_handler(ServiceError)
    def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    def db(request: Request) -> Iterator[Session]:
        session = request.app.state.session_maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def current_user(authorization: str | None = Header(default=None), session: Session = Depends(db)) -> User:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        user = services.user_for_token(session, authorization.split(" ", 1)[1].strip())
        if user is None:
            raise HTTPException(401, "invalid token")
        return user

    def roles(*allowed: str):
        def check(user: User = Depends(current_user)) -> User:
            if user.role not in allowed:
                raise HTTPException(403, f"requires role: {', '.join(allowed)}")
            return user

        return check

    # --- basics -------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/me")
    def me(user: User = Depends(current_user)) -> dict[str, str]:
        return {"id": user.id, "email": user.email, "role": user.role, "display_name": user.display_name}


    @app.post("/api/users")
    def create_user(body: UserIn, admin: User = Depends(roles("admin")), session: Session = Depends(db)) -> dict[str, str]:
        if body.role not in ("admin", "rubric_owner", "reviewer", "auditor"):
            raise HTTPException(400, "unknown role")
        user, token = services.create_user(session, body.email, body.display_name, body.role, admin.email)
        return {"id": user.id, "email": user.email, "role": user.role, "api_token": token}

    @app.get("/api/users")
    def list_users(user: User = Depends(roles("admin", "auditor")), session: Session = Depends(db)) -> list[dict[str, str]]:
        return [{"id": u.id, "email": u.email, "role": u.role, "display_name": u.display_name} for u in session.scalars(select(User))]

    # --- roles, configs, evals ---------------------------------------------


    @app.post("/api/roles")
    def create_role(body: RoleIn, admin: User = Depends(roles("admin")), session: Session = Depends(db)) -> dict[str, Any]:
        if session.get(JobRole, body.id):
            raise HTTPException(409, "role exists")
        session.add(JobRole(id=body.id, title=body.title, job_description=body.job_description))
        audit.record(session, admin.email, "role.create", "role", body.id)
        return {"id": body.id}

    @app.get("/api/roles")
    def list_roles(user: User = Depends(current_user), session: Session = Depends(db)) -> list[dict[str, Any]]:
        return [{"id": r.id, "title": r.title, "active_config_hash": r.active_config_hash} for r in session.scalars(select(JobRole))]


    def _config_view(session: Session, row: PipelineConfigRow) -> dict[str, Any]:
        loaded = services.load_config_text(row.yaml_text)
        evals = list(session.scalars(select(EvalRunRow).where(EvalRunRow.config_hash == row.hash).order_by(EvalRunRow.created_at.desc())))
        return {
            "hash": row.hash,
            "role_id": row.role_id,
            "status": row.status,
            "rubric_ref": row.rubric_ref,
            "yaml": row.yaml_text,
            "created_by": row.created_by,
            "promoted_by": row.promoted_by,
            "override_justification": row.override_justification,
            "hash_still_resolves": loaded.hash == row.hash,
            "lint": [f.model_dump() for f in lint_rubric(loaded.rubric)],
            "rubric": loaded.rubric.model_dump(mode="json"),
            "eval_runs": [{"id": e.id, "passed": e.passed, "eval_set_id": e.eval_set_id, "created_at": e.created_at.isoformat()} for e in evals],
            "eval_running": row.hash in app.state.running_evals,
        }

    @app.post("/api/configs")
    def register_config(body: ConfigIn, user: User = Depends(roles("rubric_owner", "admin")), session: Session = Depends(db)) -> dict[str, Any]:
        try:
            row, _ = services.register_config(session, body.yaml, user.email)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, f"invalid config: {e}") from e
        return _config_view(session, row)

    @app.get("/api/configs")
    def list_configs(user: User = Depends(roles("rubric_owner", "admin", "auditor")), session: Session = Depends(db)) -> list[dict[str, Any]]:
        rows = session.scalars(select(PipelineConfigRow).order_by(PipelineConfigRow.created_at.desc()))
        return [{"hash": r.hash, "role_id": r.role_id, "status": r.status, "rubric_ref": r.rubric_ref, "created_at": r.created_at.isoformat()} for r in rows]

    @app.get("/api/configs/{config_hash}")
    def get_config(config_hash: str, user: User = Depends(roles("rubric_owner", "admin", "auditor")), session: Session = Depends(db)) -> dict[str, Any]:
        row = session.get(PipelineConfigRow, config_hash)
        if row is None:
            raise HTTPException(404, "config not found")
        return _config_view(session, row)

    @app.get("/api/eval-sets")
    def list_eval_sets(user: User = Depends(roles("rubric_owner", "admin", "auditor"))) -> list[str]:
        return sorted(p.stem for p in EVAL_SETS_DIR.glob("*.jsonl"))


    def _run_eval_job(config_hash: str, eval_set: str, repeats: int, actor: str) -> None:
        session = app.state.session_maker()
        try:
            row = session.get(PipelineConfigRow, config_hash)
            loaded = services.loaded_for(row)
            es = load_eval_set(EVAL_SETS_DIR / f"{eval_set}.jsonl")
            report = run_eval(es, loaded, app.state.provider, repeats=repeats)
            report.gate = evaluate_gate(report, load_gate(GATE_PATH))
            services.store_eval_run(session, report, actor)
            session.commit()
        except Exception as e:  # recorded in the audit log; never contains candidate data (eval sets are synthetic)
            session.rollback()
            audit.record(session, actor, "eval.failed", "pipeline_config", config_hash, detail={"error": f"{type(e).__name__}: {e}"[:500]})
            session.commit()
            log.exception("eval job failed for %s", config_hash[:12])
        finally:
            session.close()
            with app.state.lock:
                app.state.running_evals.discard(config_hash)

    @app.post("/api/configs/{config_hash}/evals", status_code=202)
    def start_eval(config_hash: str, body: EvalIn, background: BackgroundTasks, user: User = Depends(roles("rubric_owner", "admin")), session: Session = Depends(db)) -> dict[str, Any]:
        row = session.get(PipelineConfigRow, config_hash)
        if row is None:
            raise HTTPException(404, "config not found")
        services.loaded_for(row)
        if not (EVAL_SETS_DIR / f"{body.eval_set}.jsonl").exists():
            raise HTTPException(404, "eval set not found")
        with app.state.lock:
            if config_hash in app.state.running_evals:
                raise HTTPException(409, "an eval is already running for this config")
            app.state.running_evals.add(config_hash)
        audit.record(session, user.email, "eval.start", "pipeline_config", config_hash, detail={"eval_set": body.eval_set})
        session.commit()
        if app.state.run_evals_inline:
            _run_eval_job(config_hash, body.eval_set, body.repeats, user.email)
        else:
            background.add_task(_run_eval_job, config_hash, body.eval_set, body.repeats, user.email)
        return {"status": "started"}

    @app.get("/api/eval-runs/{eval_run_id}")
    def get_eval_run(eval_run_id: str, user: User = Depends(roles("rubric_owner", "admin", "auditor")), session: Session = Depends(db)) -> dict[str, Any]:
        row = session.get(EvalRunRow, eval_run_id)
        if row is None:
            raise HTTPException(404, "eval run not found")
        report = EvalReport.model_validate(row.report)
        return {"id": row.id, "config_hash": row.config_hash, "passed": row.passed, "summary": report_summary(report), "gate": row.gate, "metrics": row.metrics, "counterfactual": {k: v.model_dump() for k, v in report.counterfactual.items()}, "sensitivity_redaction_off": {k: v.model_dump() for k, v in report.sensitivity_redaction_off.items()}}

    @app.get("/api/eval-diff")
    def eval_diff(before: str, after: str, user: User = Depends(roles("rubric_owner", "admin", "auditor")), session: Session = Depends(db)) -> dict[str, Any]:
        a, b = session.get(EvalRunRow, before), session.get(EvalRunRow, after)
        if a is None or b is None:
            raise HTTPException(404, "eval run not found")
        d = diff_reports(EvalReport.model_validate(a.report), EvalReport.model_validate(b.report))
        return {"diff": d.model_dump(mode="json"), "text": format_diff(d)}


    @app.post("/api/configs/{config_hash}/promote")
    def promote(config_hash: str, body: PromoteIn, admin: User = Depends(roles("admin")), session: Session = Depends(db)) -> dict[str, Any]:
        row = services.promote_config(session, config_hash, admin, override_justification=body.override_justification)
        return {"hash": row.hash, "status": row.status, "eval_run_id": row.eval_run_id, "override_justification": row.override_justification}

    # --- requisitions and applications ------------------------------------


    @app.post("/api/requisitions")
    def create_requisition(body: RequisitionIn, admin: User = Depends(roles("admin")), session: Session = Depends(db)) -> dict[str, Any]:
        req = services.create_requisition(session, body.role_id, body.opening_ref, admin.email)
        return {"id": req.id, "role_id": req.role_id, "opening_ref": req.opening_ref}

    @app.get("/api/requisitions")
    def list_requisitions(user: User = Depends(roles("admin", "reviewer", "auditor")), session: Session = Depends(db)) -> list[dict[str, Any]]:
        reqs = list(session.scalars(select(Requisition).order_by(Requisition.created_at.desc())))
        if user.role == "reviewer":
            reqs = [r for r in reqs if services.is_assigned(session, user, r.id)]
        return [{"id": r.id, "role_id": r.role_id, "opening_ref": r.opening_ref, "status": r.status, "pinned_config_hash": r.pinned_config_hash} for r in reqs]


    @app.post("/api/requisitions/{requisition_id}/reviewers")
    def assign(requisition_id: str, body: AssignIn, admin: User = Depends(roles("admin")), session: Session = Depends(db)) -> dict[str, str]:
        services.assign_reviewer(session, body.reviewer_id, requisition_id, admin.email)
        return {"status": "assigned"}


    @app.post("/api/requisitions/{requisition_id}/rescore")
    def rescore(requisition_id: str, body: RescoreIn, admin: User = Depends(roles("admin")), session: Session = Depends(db)) -> dict[str, Any]:
        runs = services.rescore_requisition(session, requisition_id, body.config_hash, admin, app.state.provider)
        return {"rescored": len(runs)}

    def _can_upload(session: Session, user: User, requisition_id: str) -> None:
        if user.role == "admin":
            return
        if user.role == "reviewer" and services.is_assigned(session, user, requisition_id):
            return
        raise HTTPException(403, "only admins or assigned reviewers can upload")

    def _application_view(app_row: Application, run: Run | None) -> dict[str, Any]:
        return {"application_id": app_row.id, "status": app_row.status, "ai_opt_out": app_row.ai_opt_out, "run_id": run.id if run else None, "run_status": run.status if run else None}

    @app.post("/api/requisitions/{requisition_id}/applications")
    async def upload(
        requisition_id: str,
        file: UploadFile = File(...),
        jurisdiction: str = Form("unknown"),
        ai_opt_out: bool = Form(False),
        notice_version: str | None = Form(None),
        user: User = Depends(current_user),
        session: Session = Depends(db),
    ) -> dict[str, Any]:
        _can_upload(session, user, requisition_id)
        data = await file.read()
        app_row, run = services.submit_application(session, requisition_id=requisition_id, data=data, filename=file.filename or "upload", actor=user.email, provider=app.state.provider, jurisdiction=jurisdiction, ai_opt_out=ai_opt_out, notice_version=notice_version)
        return _application_view(app_row, run)

    @app.post("/api/requisitions/{requisition_id}/import")
    async def import_zip(requisition_id: str, file: UploadFile = File(...), user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        """A zip of resumes, optionally with manifest.csv (filename, jurisdiction, ai_opt_out, notice_version)."""
        _can_upload(session, user, requisition_id)
        try:
            archive = zipfile.ZipFile(io.BytesIO(await file.read()))
        except zipfile.BadZipFile as e:
            raise HTTPException(400, "not a zip file") from e
        manifest: dict[str, dict[str, str]] = {}
        if "manifest.csv" in archive.namelist():
            for row in csv.DictReader(io.StringIO(archive.read("manifest.csv").decode("utf-8"))):
                manifest[row["filename"]] = row
        results = []
        for name in archive.namelist():
            if name.endswith("/") or name == "manifest.csv" or Path(name).name.startswith("."):
                continue
            meta = manifest.get(name, {})
            app_row, run = services.submit_application(
                session,
                requisition_id=requisition_id,
                data=archive.read(name),
                filename=Path(name).name,
                actor=user.email,
                provider=app.state.provider,
                source="zip_import",
                jurisdiction=meta.get("jurisdiction") or "unknown",
                ai_opt_out=(meta.get("ai_opt_out", "").strip().lower() in ("1", "true", "yes")),
                notice_version=meta.get("notice_version") or None,
            )
            results.append({"file": name, **_application_view(app_row, run)})
        return {"imported": len(results), "applications": results}

    @app.get("/api/requisitions/{requisition_id}/queue")
    def queue(requisition_id: str, user: User = Depends(roles("admin", "reviewer", "auditor")), session: Session = Depends(db)) -> list[dict[str, Any]]:
        return services.review_queue(session, requisition_id, user)

    def _app_for(session: Session, application_id: str, user: User) -> Application:
        app_row = session.get(Application, application_id)
        if app_row is None:
            raise HTTPException(404, "application not found")
        if user.role == "reviewer" and not services.is_assigned(session, user, app_row.requisition_id):
            raise HTTPException(403, "not assigned to this requisition")
        if user.role not in ("reviewer", "admin", "auditor"):
            raise HTTPException(403, "role can't view applications")
        return app_row

    @app.get("/api/applications/{application_id}")
    def get_application(application_id: str, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        app_row = _app_for(session, application_id, user)
        run = services.latest_run(session, app_row.id)
        doc = session.scalars(select(Document).where(Document.application_id == app_row.id).order_by(Document.created_at.desc())).first()
        rcfg = services.review_config(session, session.get(Requisition, app_row.requisition_id))
        revealed = any(e.event == "scores_revealed" for e in services.events_for(session, app_row.id, user.id))
        return {
            **_application_view(app_row, run),
            "requisition_id": app_row.requisition_id,
            "document_id": doc.id if doc else None,
            "has_render": bool(doc and doc.render_blob_ref),
            "blind_first_look": rcfg.blind_first_look,
            "scores_revealed": revealed,
            "opened_by_me": any(e.event == "opened" for e in services.events_for(session, app_row.id, user.id)),
            "decisions": [{"decision": d.decision, "reason": d.reason, "override": d.override, "suggested_tier": d.suggested_tier, "reviewer_id": d.reviewer_id, "at": d.at.isoformat()} for d in session.scalars(select(Decision).where(Decision.application_id == app_row.id).order_by(Decision.at))],
        }


    @app.post("/api/applications/{application_id}/events")
    def post_event(application_id: str, body: EventIn, user: User = Depends(roles("reviewer")), session: Session = Depends(db)) -> dict[str, str]:
        if body.event == "opened":
            raise HTTPException(400, "`opened` is recorded when the document is fetched")
        ev = services.record_review_event(session, application_id, user, body.event, body.detail)
        return {"id": ev.id}


    @app.post("/api/applications/{application_id}/decision")
    def decide(application_id: str, body: DecisionIn, user: User = Depends(roles("reviewer")), session: Session = Depends(db)) -> dict[str, Any]:
        d = services.record_decision(session, application_id, user, body.decision, body.reason, body.override)
        return {"id": d.id, "decision": d.decision, "suggested_tier": d.suggested_tier}

    @app.post("/api/applications/{application_id}/opt-out")
    def opt_out(application_id: str, admin: User = Depends(roles("admin")), session: Session = Depends(db)) -> dict[str, str]:
        services.record_opt_out(session, application_id, admin.email)
        return {"status": "opted_out"}

    @app.get("/api/documents/{document_id}/render")
    def render(document_id: str, user: User = Depends(current_user), session: Session = Depends(db)) -> Response:
        doc = session.get(Document, document_id)
        if doc is None:
            raise HTTPException(404, "document not found")
        app_row = _app_for(session, doc.application_id, user)
        ref = doc.render_blob_ref or doc.blob_ref
        if user.role == "reviewer":
            # Fetching the document is what counts as opening it: a decision needs this event.
            services.record_review_event(session, app_row.id, user, "opened", {"document_id": doc.id})
        else:
            audit.record(session, user.email, "document.read", "document", doc.id)
        media = "application/pdf" if doc.render_blob_ref or doc.mime == "application/pdf" else "application/octet-stream"
        return Response(content=blobs.get(ref), media_type=media, headers={"Cache-Control": "no-store"})

    @app.get("/api/runs/{run_id}/trace")
    def trace(run_id: str, user: User = Depends(current_user), session: Session = Depends(db)) -> dict[str, Any]:
        return services.run_trace(session, run_id, user)

    @app.get("/api/runs/{run_id}/redaction-map")
    def get_redaction_map(run_id: str, user: User = Depends(roles("reviewer", "admin", "auditor")), session: Session = Depends(db)) -> list[Any]:
        return services.redaction_map(session, run_id, user)

    # --- oversight ---------------------------------------------------------

    @app.get("/api/alerts")
    def get_alerts(user: User = Depends(roles("admin", "reviewer", "auditor")), session: Session = Depends(db)) -> list[dict[str, Any]]:
        items = services.alerts(session)
        if user.role == "reviewer":
            items = [a for a in items if services.is_assigned(session, user, a["requisition_id"])]
        return items

    @app.get("/api/metrics/reviewers")
    def get_reviewer_metrics(user: User = Depends(roles("admin", "auditor")), session: Session = Depends(db)) -> list[dict[str, Any]]:
        return services.reviewer_metrics(session)

    @app.get("/api/export/applications/{application_id}")
    def export_application(application_id: str, user: User = Depends(roles("admin", "auditor")), session: Session = Depends(db)) -> dict[str, Any]:
        return services.export_application(session, application_id, user)

    @app.get("/api/export/requisitions/{requisition_id}")
    def export_requisition(requisition_id: str, user: User = Depends(roles("admin", "auditor")), session: Session = Depends(db)) -> dict[str, Any]:
        apps = list(session.scalars(select(Application).where(Application.requisition_id == requisition_id)))
        return {"requisition_id": requisition_id, "applications": [services.export_application(session, a.id, user) for a in apps]}


    @app.post("/api/candidates/{candidate_id}/delete")
    def delete_candidate(candidate_id: str, body: DeleteIn, admin: User = Depends(roles("admin")), session: Session = Depends(db)) -> dict[str, Any]:
        if not body.reason.strip():
            raise HTTPException(400, "a reason is required")
        return {"deleted": services.delete_candidate(session, candidate_id, admin.email, body.reason)}

    @app.get("/api/audit")
    def get_audit(entity_id: str | None = None, limit: int = 200, user: User = Depends(roles("admin", "auditor")), session: Session = Depends(db)) -> list[dict[str, Any]]:
        q = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 1000))
        if entity_id:
            q = q.where(AuditLog.entity_id == entity_id)
        return [{"id": a.id, "actor": a.actor, "action": a.action, "entity_type": a.entity_type, "entity_id": a.entity_id, "at": a.at.isoformat(), "detail": a.detail} for a in session.scalars(q)]

    # --- frontend ----------------------------------------------------------

    if FRONTEND_DIST.exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(FRONTEND_DIST / "index.html")

    return app


app = create_app()
