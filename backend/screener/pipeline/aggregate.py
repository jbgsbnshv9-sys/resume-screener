"""Node 5: aggregate (deterministic). Aggregation happens here, never in a model (principle 4).

weighted score = sum(w_i * level_i) / (3 * sum(w_i)), in [0, 1].
A must-have criterion at level 0 caps the tier at `must_have_cap`.
A criterion without a valid score counts as 0 and is already flagged.
Any flag from any node marks the candidate "needs attention".
The tier is a suggestion that orders the review queue; it is never a decision.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

from screener.config import TIER_ORDER, AggregateConfig, Rubric, ScoreConfig
from screener.hashing import hash_value
from screener.pipeline.score import CriterionScore
from screener.pipeline.types import Flag, FlagType, NodeOutput


class Contribution(BaseModel):
    criterion: str
    weight: float
    level: int | None
    must_have: bool


class AggregateResult(BaseModel):
    weighted_score: float
    tier: str
    uncapped_tier: str
    capped_by: list[str] = Field(default_factory=list)
    needs_attention: bool
    contributions: list[Contribution]
    score_range: tuple[float, float]
    flags: list[Flag] = Field(default_factory=list)


def band(score: float, config: AggregateConfig) -> str:
    if score >= config.tiers.strong:
        return "strong"
    if score >= config.tiers.review:
        return "review"
    return "weak"


def _cap(tier: str, cap: str) -> str:
    return tier if TIER_ORDER.index(tier) <= TIER_ORDER.index(cap) else cap


def weighted(levels: dict[str, int | None], rubric: Rubric) -> float:
    total_w = sum(c.weight for c in rubric.criteria)
    return sum(c.weight * (levels.get(c.id) or 0) for c in rubric.criteria) / (3 * total_w)


def tier_for(levels: dict[str, int | None], rubric: Rubric, config: AggregateConfig) -> tuple[float, str, str, list[str]]:
    score = weighted(levels, rubric)
    uncapped = band(score, config)
    capped_by = [c.id for c in rubric.criteria if c.must_have and (levels.get(c.id) or 0) == 0]
    tier = _cap(uncapped, config.must_have_cap) if capped_by else uncapped
    return score, tier, uncapped, capped_by


def run_aggregate(scores: list[CriterionScore], upstream_flags: list[Flag], rubric: Rubric, config: AggregateConfig, score_config: ScoreConfig) -> tuple[AggregateResult, NodeOutput]:
    started = time.monotonic()
    by_id = {s.criterion: s for s in scores}
    levels = {c.id: by_id[c.id].final_level if c.id in by_id else None for c in rubric.criteria}
    score, tier, uncapped, capped_by = tier_for(levels, rubric, config)

    lo_levels = {cid: min(by_id[cid].valid_levels(), default=0) for cid in levels if cid in by_id}
    hi_levels = {cid: max(by_id[cid].valid_levels(), default=0) for cid in levels if cid in by_id}
    lo, lo_tier, _, _ = tier_for(lo_levels, rubric, config)
    hi, hi_tier, _, _ = tier_for(hi_levels, rubric, config)

    flags: list[Flag] = []
    if score_config.flag_tier_straddle and lo_tier != hi_tier:
        flags.append(Flag(node="aggregate", type=FlagType.TIER_STRADDLE, detail=f"sample disagreement spans tiers {lo_tier}..{hi_tier} (scores {lo:.2f}..{hi:.2f})"))
    all_flags = [*upstream_flags, *flags]
    result = AggregateResult(
        weighted_score=round(score, 6),
        tier=tier,
        uncapped_tier=uncapped,
        capped_by=capped_by,
        needs_attention=bool(all_flags),
        contributions=[Contribution(criterion=c.id, weight=c.weight, level=levels[c.id], must_have=c.must_have) for c in rubric.criteria],
        score_range=(round(lo, 6), round(hi, 6)),
        flags=all_flags,
    )
    node = NodeOutput(
        node="aggregate",
        input_hash=hash_value({"scores": [s.model_dump(mode="json") for s in scores], "flags": [f.model_dump(mode="json") for f in upstream_flags]}),
        output=result.model_dump(mode="json"),
        config=config.model_dump(mode="json"),
        flags=flags,
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return result, node
