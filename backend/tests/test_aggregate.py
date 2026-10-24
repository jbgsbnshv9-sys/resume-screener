import pytest

from screener.config import AggregateConfig, ScoreConfig, load_rubric
from screener.pipeline.aggregate import run_aggregate
from screener.pipeline.score import CriterionScore, Sample
from screener.pipeline.types import Flag, FlagType

RUBRIC = load_rubric("backend-engineer-2026q4@v1")
AGG = AggregateConfig(tiers={"strong": 0.75, "review": 0.45})
SCORE = ScoreConfig(model="t")


def _scores(levels: dict[str, int | list[int]]):
    out = []
    for c in RUBRIC.criteria:
        lv = levels.get(c.id, 0)
        samples_levels = lv if isinstance(lv, list) else [lv]
        samples = [Sample(index=i, claimed_level=x, level=x, valid=True) for i, x in enumerate(samples_levels)]
        out.append(CriterionScore(criterion=c.id, samples=samples, final_level=sorted(samples_levels)[(len(samples_levels) - 1) // 2], verified=True, unstable=False))
    return out


def test_weighted_score_formula():
    result, _ = run_aggregate(_scores({c.id: 3 for c in RUBRIC.criteria}), [], RUBRIC, AGG, SCORE)
    assert result.weighted_score == pytest.approx(1.0)
    assert result.tier == "strong" and not result.needs_attention
    half, _ = run_aggregate(_scores({"backend_development": 3}), [], RUBRIC, AGG, SCORE)
    total = sum(c.weight for c in RUBRIC.criteria)
    assert half.weighted_score == pytest.approx(3 * 3 / (3 * total))


def test_must_have_at_zero_caps_tier_at_review():
    levels = {c.id: 3 for c in RUBRIC.criteria}
    levels["backend_development"] = 0
    result, _ = run_aggregate(_scores(levels), [], RUBRIC, AGG, SCORE)
    assert result.uncapped_tier == "review" or result.uncapped_tier == "strong"
    assert result.tier in ("review", "weak") and result.capped_by == ["backend_development"]
    levels = {c.id: 3 for c in RUBRIC.criteria if c.id != "backend_development"}
    levels["backend_development"] = 0
    agg = AggregateConfig(tiers={"strong": 0.5, "review": 0.3})
    result, _ = run_aggregate(_scores(levels), [], RUBRIC, agg, SCORE)
    assert result.uncapped_tier == "strong" and result.tier == "review"


def test_upstream_flag_means_needs_attention():
    flag = Flag(node="intake", type=FlagType.HIDDEN_TEXT, detail="x")
    result, _ = run_aggregate(_scores({c.id: 3 for c in RUBRIC.criteria}), [flag], RUBRIC, AGG, SCORE)
    assert result.needs_attention and result.tier == "strong"


def test_samples_straddling_a_tier_boundary_are_flagged():
    levels = {c.id: 2 for c in RUBRIC.criteria}
    levels["backend_development"] = [2, 3, 3]
    levels["distributed_systems"] = [2, 3, 3]
    levels["data_storage"] = [2, 3, 3]
    result, node = run_aggregate(_scores(levels), [], RUBRIC, AGG, SCORE)
    lo, hi = result.score_range
    assert lo < 0.75 <= hi
    assert any(f.type == FlagType.TIER_STRADDLE for f in node.flags) and result.needs_attention
