"""Append-only audit trail. Every state change an actor makes goes through `record`."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from screener.hashing import hash_value
from screener.models.db import AuditLog


def record(session: Session, actor: str, action: str, entity_type: str, entity_id: str, *, before: Any = None, after: Any = None, detail: dict[str, Any] | None = None) -> AuditLog:
    entry = AuditLog(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_hash=hash_value(before) if before is not None else None,
        after_hash=hash_value(after) if after is not None else None,
        detail=detail or {},
    )
    session.add(entry)
    return entry
