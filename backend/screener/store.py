"""Persisting configs and runs."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from screener import audit
from screener.config import LoadedConfig
from screener.models.db import CriterionScoreRow, FlagRow, JobRole, NodeOutputRow, PipelineConfigRow, RedactionMapRow, RubricRow, Run, utcnow
from screener.pipeline.runner import RunArtifacts, RunResult


def ensure_role(session: Session, role_id: str, title: str | None = None) -> JobRole:
    role = session.get(JobRole, role_id)
    if role is None:
        role = JobRole(id=role_id, title=title or role_id)
        session.add(role)
        session.flush()
    return role


def ensure_config(session: Session, loaded: LoadedConfig, actor: str) -> PipelineConfigRow:
    """Store the rubric and config if new. Both are immutable once stored (principle 5)."""
    rubric = loaded.rubric
    ensure_role(session, rubric.role)
    existing_rubric = session.get(RubricRow, rubric.ref)
    if existing_rubric is None:
        session.add(
            RubricRow(
                ref=rubric.ref,
                rubric_id=rubric.id,
                version=rubric.version,
                role_id=rubric.role,
                criteria=[c.model_dump(mode="json") for c in rubric.criteria],
                lint_acknowledgments=[a.model_dump(mode="json") for a in rubric.lint_acknowledgments],
                content_hash=rubric.content_hash,
                author=actor,
            )
        )
        audit.record(session, actor, "rubric.create", "rubric", rubric.ref, after=rubric.model_dump(mode="json"))
    elif existing_rubric.content_hash != rubric.content_hash:
        raise ValueError(f"rubric {rubric.ref} already exists with different content; bump the version")
    row = session.get(PipelineConfigRow, loaded.hash)
    if row is None:
        row = PipelineConfigRow(hash=loaded.hash, role_id=rubric.role, yaml_text=loaded.yaml_text, rubric_ref=rubric.ref, status="draft", created_by=actor)
        session.add(row)
        audit.record(session, actor, "config.create", "pipeline_config", loaded.hash, after={"yaml": loaded.yaml_text})
    session.flush()
    return row


def save_run(session: Session, run: RunResult, artifacts: RunArtifacts, *, application_id: str | None = None, document_id: str | None = None, supersedes: str | None = None) -> Run:
    agg = run.aggregate
    row = Run(
        application_id=application_id,
        document_id=document_id,
        config_hash=run.config_hash,
        status=run.status.value,
        stop_reason=run.stop_reason,
        tier=agg.tier if agg else None,
        weighted_score=agg.weighted_score if agg else None,
        needs_attention=bool(run.flags) or run.status != "completed",
        supersedes_run_id=supersedes,
        finished_at=utcnow(),
        result=run.model_dump(mode="json"),
    )
    session.add(row)
    session.flush()
    for seq, node in enumerate(run.nodes):
        session.add(
            NodeOutputRow(
                run_id=row.id,
                seq=seq,
                node=node.node,
                input_hash=node.input_hash,
                output=node.output,
                config=node.config,
                models=sorted({c.model for c in node.llm_calls}),
                prompt_hashes=sorted({c.prompt_hash for c in node.llm_calls}),
                input_tokens=node.usage.input_tokens,
                output_tokens=node.usage.output_tokens,
                latency_ms=node.latency_ms,
                llm_calls=[c.model_dump(mode="json") for c in node.llm_calls],
            )
        )
    for score in run.scores:
        session.add(
            CriterionScoreRow(
                run_id=row.id,
                criterion=score.criterion,
                samples=[s.model_dump(mode="json") for s in score.samples],
                final_level=score.final_level,
                evidence=[e.model_dump(mode="json") for e in score.evidence],
                verified=score.verified,
                unstable=score.unstable,
            )
        )
    for flag in run.flags:
        session.add(FlagRow(run_id=row.id, node=flag.node, type=flag.type.value, detail=flag.detail, criterion=flag.criterion))
    if artifacts.redaction_map is not None:
        session.add(RedactionMapRow(run_id=row.id, redactions=[r.model_dump(mode="json") for r in artifacts.redaction_map.redactions]))
    session.flush()
    return row


def load_run(session: Session, run_id: str) -> RunResult | None:
    row = session.get(Run, run_id)
    return RunResult.model_validate(row.result) if row else None


def latest_run(session: Session, application_id: str) -> Run | None:
    return session.scalars(select(Run).where(Run.application_id == application_id).order_by(Run.started_at.desc())).first()
