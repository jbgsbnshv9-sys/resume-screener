"""Process-level settings read from the environment."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql+psycopg://screener:screener@localhost:5432/screener")


def configs_dir() -> Path:
    return Path(os.environ.get("SCREENER_CONFIGS_DIR", REPO_ROOT / "configs"))


def blob_dir() -> Path:
    return Path(os.environ.get("SCREENER_BLOB_DIR", REPO_ROOT / "var" / "blobs"))


def live_llm_enabled() -> bool:
    return os.environ.get("SCREENER_LIVE_LLM") == "1"
