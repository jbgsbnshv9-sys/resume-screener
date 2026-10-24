"""SQLAlchemy models (SPEC.md, Data model).

Node outputs live in JSONB. The redaction map has its own table so access to it
can be granted separately. The audit log is append-only: the ORM refuses
updates and deletes, and the migration installs a Postgres trigger that does
the same for any client.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSONType = JSON().with_variant(JSONB(), "postgresql")


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONType, list[Any]: JSONType}


def _ts(**kw) -> Any:
    return mapped_column(DateTime(timezone=True), default=utcnow, **kw)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))
    api_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (CheckConstraint("role in ('admin','rubric_owner','reviewer','auditor')", name="ck_users_role"),)


class JobRole(Base):
    __tablename__ = "roles"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    job_description: Mapped[str] = mapped_column(Text, default="")
    active_config_hash: Mapped[str | None] = mapped_column(ForeignKey("pipeline_configs.hash", use_alter=True, name="fk_roles_active_config"), nullable=True)


class RubricRow(Base):
    __tablename__ = "rubrics"
    ref: Mapped[str] = mapped_column(String(200), primary_key=True)
    rubric_id: Mapped[str] = mapped_column(String(150))
    version: Mapped[int] = mapped_column(Integer)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"))
    criteria: Mapped[list[Any]] = mapped_column(JSONType)
    lint_acknowledgments: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    content_hash: Mapped[str] = mapped_column(String(64))
    author: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (UniqueConstraint("rubric_id", "version"),)


class PipelineConfigRow(Base):
    __tablename__ = "pipeline_configs"
    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"))
    yaml_text: Mapped[str] = mapped_column(Text)
    rubric_ref: Mapped[str] = mapped_column(ForeignKey("rubrics.ref"))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_by: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = _ts()
    promoted_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    eval_run_id: Mapped[str | None] = mapped_column(ForeignKey("eval_runs.id", use_alter=True, name="fk_configs_eval_run"), nullable=True)
    override_justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (CheckConstraint("status in ('draft','validated','active','retired')", name="ck_configs_status"),)


class Requisition(Base):
    __tablename__ = "requisitions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"))
    opening_ref: Mapped[str] = mapped_column(String(200))
    pinned_config_hash: Mapped[str | None] = mapped_column(ForeignKey("pipeline_configs.hash"), nullable=True)
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = _ts()


class Candidate(Base):
    __tablename__ = "candidates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Keyed hash of the normalized email, for dedup without storing the email here.
    person_key: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(50), default="upload")
    jurisdiction: Mapped[str] = mapped_column(String(20), default="unknown")
    created_at: Mapped[datetime] = _ts()
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Application(Base):
    __tablename__ = "applications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"))
    requisition_id: Mapped[str] = mapped_column(ForeignKey("requisitions.id"))
    applied_at: Mapped[datetime] = _ts()
    # pending -> in_review -> decided; manual_only for opt-outs and runs that stopped early.
    status: Mapped[str] = mapped_column(String(20), default="pending")
    ai_opt_out: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("candidate_id", "requisition_id"),)

    candidate: Mapped[Candidate] = relationship()
    requisition: Mapped[Requisition] = relationship()


class CandidateNotice(Base):
    __tablename__ = "candidate_notices"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"))
    notice_version: Mapped[str] = mapped_column(String(50))
    channel: Mapped[str] = mapped_column(String(50))
    delivered_at: Mapped[datetime] = _ts()


class ConsentRecord(Base):
    __tablename__ = "consent_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"))
    type: Mapped[str] = mapped_column(String(20))
    recorded_by: Mapped[str] = mapped_column(String(320))
    recorded_at: Mapped[datetime] = _ts()
    __table_args__ = (CheckConstraint("type in ('consent','opt_out')", name="ck_consent_type"),)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"))
    filename: Mapped[str] = mapped_column(String(300))
    blob_ref: Mapped[str] = mapped_column(String(300))
    render_blob_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    mime: Mapped[str] = mapped_column(String(120))
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts()


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    config_hash: Mapped[str] = mapped_column(ForeignKey("pipeline_configs.hash"))
    status: Mapped[str] = mapped_column(String(20))
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    tier: Mapped[str | None] = mapped_column(String(10), nullable=True)
    weighted_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    needs_attention: Mapped[bool] = mapped_column(Boolean, default=False)
    supersedes_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The full RunResult, for recompute and the trace viewer.
    result: Mapped[dict[str, Any]] = mapped_column(JSONType)

    nodes: Mapped[list[NodeOutputRow]] = relationship(back_populates="run", order_by="NodeOutputRow.seq")
    scores: Mapped[list[CriterionScoreRow]] = relationship(back_populates="run")
    flags: Mapped[list[FlagRow]] = relationship(back_populates="run")


class NodeOutputRow(Base):
    __tablename__ = "node_outputs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    node: Mapped[str] = mapped_column(String(20))
    input_hash: Mapped[str] = mapped_column(String(64))
    output: Mapped[dict[str, Any]] = mapped_column(JSONType)
    config: Mapped[dict[str, Any]] = mapped_column(JSONType)
    models: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    prompt_hashes: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[list[Any]] = mapped_column(JSONType, default=list)

    run: Mapped[Run] = relationship(back_populates="nodes")


class CriterionScoreRow(Base):
    __tablename__ = "criterion_scores"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    criterion: Mapped[str] = mapped_column(String(100))
    samples: Mapped[list[Any]] = mapped_column(JSONType)
    final_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence: Mapped[list[Any]] = mapped_column(JSONType)
    verified: Mapped[bool] = mapped_column(Boolean)
    unstable: Mapped[bool] = mapped_column(Boolean)

    run: Mapped[Run] = relationship(back_populates="scores")


class FlagRow(Base):
    __tablename__ = "flags"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    node: Mapped[str] = mapped_column(String(20))
    type: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(Text)
    criterion: Mapped[str | None] = mapped_column(String(100), nullable=True)

    run: Mapped[Run] = relationship(back_populates="flags")


class RedactionMapRow(Base):
    """Restricted: readable by reviewers, admins and auditors only."""

    __tablename__ = "redaction_maps"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    redactions: Mapped[list[Any]] = mapped_column(JSONType)


class ReviewerAssignment(Base):
    __tablename__ = "reviewer_assignments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    requisition_id: Mapped[str] = mapped_column(ForeignKey("requisitions.id"))
    assigned_at: Mapped[datetime] = _ts()
    __table_args__ = (UniqueConstraint("reviewer_id", "requisition_id"),)


class ReviewEvent(Base):
    __tablename__ = "review_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    event: Mapped[str] = mapped_column(String(30))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    at: Mapped[datetime] = _ts()
    __table_args__ = (CheckConstraint("event in ('opened','viewed_page','scores_revealed','closed')", name="ck_review_event"),)


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str] = mapped_column(Text)
    suggested_tier: Mapped[str | None] = mapped_column(String(10), nullable=True)
    override: Mapped[bool] = mapped_column(Boolean)
    at: Mapped[datetime] = _ts()
    __table_args__ = (
        CheckConstraint("decision in ('advance','hold','decline')", name="ck_decision_value"),
        CheckConstraint("length(trim(reason)) > 0", name="ck_decision_reason"),
    )


class EvalSetRow(Base):
    __tablename__ = "eval_sets"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    role_id: Mapped[str] = mapped_column(String(100))
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts()


class EvalCaseRow(Base):
    __tablename__ = "eval_cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    eval_set_id: Mapped[str] = mapped_column(ForeignKey("eval_sets.id"), index=True)
    case_id: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType)
    labels: Mapped[list[Any]] = mapped_column(JSONType)
    parent_case_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attribute: Mapped[str | None] = mapped_column(String(50), nullable=True)
    allowed_tags: Mapped[list[Any]] = mapped_column(JSONType, default=list)


class EvalRunRow(Base):
    __tablename__ = "eval_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    eval_set_id: Mapped[str] = mapped_column(String(200))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONType)
    gate: Mapped[dict[str, Any]] = mapped_column(JSONType)
    passed: Mapped[bool] = mapped_column(Boolean)
    report: Mapped[dict[str, Any]] = mapped_column(JSONType)
    created_by: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = _ts()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str] = mapped_column(String(200))
    before_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    at: Mapped[datetime] = _ts()


class AppendOnlyViolation(Exception):
    pass


@event.listens_for(AuditLog, "before_update")
def _no_update(mapper, connection, target) -> None:
    raise AppendOnlyViolation("audit_log is append-only")


@event.listens_for(AuditLog, "before_delete")
def _no_delete(mapper, connection, target) -> None:
    raise AppendOnlyViolation("audit_log is append-only")


# Raw SQL for the Postgres trigger; installed by the initial migration.
AUDIT_TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only();
"""
