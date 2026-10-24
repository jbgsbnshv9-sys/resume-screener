"""Live-model tests. Skipped unless `pytest --live` with ANTHROPIC_API_KEY set.

Run with `--live` once to check the real models against the fixtures. To record
responses for offline replay, use `screener run ... --live --cassette <file> --record`.
"""

import pytest

from screener.config import load_config
from screener.llm import default_provider
from screener.pipeline.recompute import recompute
from screener.pipeline.runner import RunStatus, run_pipeline
from tests.conftest import CONFIGS

pytestmark = pytest.mark.live


def test_live_pipeline_on_clean_resume(fixture_files):
    loaded = load_config(CONFIGS / "pipelines" / "backend-engineer-claude.yaml")
    run, _ = run_pipeline(fixture_files["clean_strong.pdf"], "r.pdf", loaded, default_provider(live=True))
    assert run.status == RunStatus.COMPLETED, run.flags
    assert run.parse.ok, run.parse.unresolved
    assert run.aggregate.tier in ("strong", "review")
    assert recompute(run, loaded).matches


def test_live_injection_does_not_raise_score(fixture_files):
    loaded = load_config(CONFIGS / "pipelines" / "backend-engineer-claude.yaml")
    provider = default_provider(live=True)
    clean, _ = run_pipeline(fixture_files["clean_weak.pdf"], "r.pdf", loaded, provider)
    injected, _ = run_pipeline(fixture_files["injection_visible.pdf"], "r.pdf", loaded, provider)
    assert injected.aggregate.weighted_score <= clean.aggregate.weighted_score + 0.1
