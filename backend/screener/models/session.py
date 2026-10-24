"""Engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from screener import settings


@cache
def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or settings.database_url(), pool_pre_ping=True)


def session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    session = session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
