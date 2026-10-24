from screener.config import ScoreConfig, load_rubric
from screener.pipeline.score import run_score
from screener.pipeline.types import FlagType

TEXT = "EXPERIENCE\nEngineer — [ORG] | Jan 2020 – Present\n• Designed and owned a production order service in Go.\nSKILLS\nGo, Kafka, PostgreSQL"
ENTRY = (TEXT.index("Engineer"), TEXT.index("SKILLS") - 1)
RUBRIC = load_rubric("backend-engineer-2026q4@v1")


def _run(script, samples=3, threshold=1):
    from tests.conftest import ScriptedClient

    client = ScriptedClient(script)
    cfg = ScoreConfig(model="test", samples=samples, instability_threshold=threshold)
    scores, node = run_score(TEXT, [ENTRY], RUBRIC, cfg, lambda m: client)
    return {s.criterion: s for s in scores}, node, client


def test_level_zero_needs_no_evidence():
    scores, node, _ = _run(lambda r: {"level": 0, "evidence": [], "rationale": "none"})
    assert all(s.final_level == 0 and s.verified for s in scores.values())
    assert node.flags == []


def test_one_call_per_criterion_per_sample_and_resume_in_user_turn():
    scores, node, client = _run(lambda r: {"level": 0, "evidence": [], "rationale": ""})
    assert len(client.requests) == len(RUBRIC.criteria) * 3
    assert all(TEXT not in r.system and r.user_blocks[0].cache for r in client.requests)
    assert all(len(r.user_blocks) == 2 and "<criterion>" in r.user_blocks[1].text for r in client.requests)
    assert len(node.llm_calls) == len(client.requests)


def test_unverified_quote_invalidates_sample_and_is_flagged():
    good = {"level": 2, "evidence": ["Designed and owned a production order service in Go."], "rationale": ""}
    answers = {0: {"level": 3, "evidence": ["Invented Kubernetes."], "rationale": ""}, 1: good, 2: good}
    scores, node, _ = _run(lambda r: answers[r.sample_index] if "Backend service" in r.user_blocks[1].text else {"level": 0, "evidence": [], "rationale": ""})
    s = scores["backend_development"]
    assert s.final_level == 2 and not s.verified
    assert s.samples[0].invalid_reason == "unverified_quote"
    assert any(f.type == FlagType.UNVERIFIED_EVIDENCE and f.criterion == "backend_development" for f in node.flags)


def test_positive_level_without_evidence_is_invalid():
    scores, node, _ = _run(lambda r: {"level": 2, "evidence": [], "rationale": ""}, samples=1)
    assert all(s.final_level is None for s in scores.values())
    assert any(f.type == FlagType.UNVERIFIED_EVIDENCE for f in node.flags)


def test_skills_only_evidence_caps_at_level_one():
    scores, _, _ = _run(lambda r: {"level": 3, "evidence": ["Go, Kafka, PostgreSQL"], "rationale": ""}, samples=1)
    s = scores["backend_development"]
    assert s.final_level == 1 and s.samples[0].capped


def test_entry_evidence_allows_high_levels():
    scores, _, _ = _run(lambda r: {"level": 3, "evidence": ["Designed and owned a production order service in Go."], "rationale": ""}, samples=1)
    assert scores["backend_development"].final_level == 3


def test_spread_beyond_threshold_is_unstable_and_median_is_low_median():
    by_sample = {0: 0, 1: 3, 2: 2}
    quote = "Designed and owned a production order service in Go."
    scores, node, _ = _run(lambda r: (lambda lv: {"level": lv, "evidence": [quote] if lv else [], "rationale": ""})(by_sample[r.sample_index]))
    s = scores["backend_development"]
    assert s.unstable and s.final_level == 2
    assert any(f.type == FlagType.UNSTABLE_CRITERION for f in node.flags)


def test_refusal_sample_is_flagged():
    from tests.conftest import ScriptedClient

    client = ScriptedClient(lambda r: "", stop_reason="refusal")
    scores, node = run_score(TEXT, [ENTRY], RUBRIC, ScoreConfig(model="t", samples=1), lambda m: client)
    assert any(f.type == FlagType.MODEL_REFUSAL for f in node.flags)
    assert all(s.final_level is None for s in scores)
