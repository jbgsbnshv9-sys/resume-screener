from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from screener import settings
from screener.config import load_config
from screener.llm import LLMRequest, LLMResponse, default_provider, response_from_raw

CONFIGS = settings.REPO_ROOT / "configs"
# DOCX intake renders through LibreOffice; the Docker image installs it. Tests that need the
# conversion skip (with this reason) only where it isn't installed; the fallback is tested everywhere.
requires_soffice = pytest.mark.skipif(not (shutil.which("soffice") or shutil.which("libreoffice")), reason="LibreOffice not installed")
SIM_CONFIG = CONFIGS / "pipelines" / "backend-engineer-sim.yaml"


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="run tests that call the real Anthropic API")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live"):
        skip = pytest.mark.skip(reason="needs --live and ANTHROPIC_API_KEY")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def sim_config():
    return load_config(SIM_CONFIG)


@pytest.fixture(scope="session")
def provider():
    return default_provider(live=False)


@pytest.fixture(scope="session")
def fixture_files():
    from screener.synthetic.fixtures import fixture_files as build

    return build()


class ScriptedClient:
    """Returns whatever `script(request)` returns, wrapped as an API response."""

    def __init__(self, script: Callable[[LLMRequest], dict[str, Any] | str], stop_reason: str = "end_turn"):
        self.script = script
        self.stop_reason = stop_reason
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        out = self.script(request)
        text = out if isinstance(out, str) else json.dumps(out)
        raw = {"id": "msg_test", "model": request.model, "content": [{"type": "text", "text": text}], "stop_reason": self.stop_reason, "usage": {"input_tokens": 10, "output_tokens": 5}}
        return response_from_raw(request.model, raw, 1)


@pytest.fixture
def scripted():
    return ScriptedClient


def _pg_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def db_engine(tmp_path):
    """A fresh database per test: Postgres via migrations when TEST_DATABASE_URL is set, else SQLite."""
    from sqlalchemy import create_engine, text

    url = _pg_url()
    if url:
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        from alembic import command
        from alembic.config import Config

        backend = Path(__file__).resolve().parents[1]
        cfg = Config(str(backend / "alembic.ini"))
        cfg.set_main_option("script_location", str(backend / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
    else:
        from screener.models.db import Base

        engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
        Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def blob_dir(tmp_path, monkeypatch):
    d = tmp_path / "blobs"
    monkeypatch.setenv("SCREENER_BLOB_DIR", str(d))
    return d
