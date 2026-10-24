"""Principle 6: everything downstream of the model is recomputable from stored data.

Given a stored run, re-derive evidence verification, per-criterion medians,
flags, and the aggregate tier from the recorded raw responses alone, and report
any difference from what was stored. No model is called.
"""

from __future__ import annotations

from pydantic import BaseModel

from screener.config import LoadedConfig
from screener.llm import response_from_raw
from screener.pipeline.aggregate import run_aggregate
from screener.pipeline.parse import ParsedRecord, entry_spans
from screener.pipeline.redact import OffsetMap
from screener.pipeline.runner import RunResult
from screener.pipeline.score import evaluate_sample, summarize
from screener.pipeline.spans import QuoteLocator, Span
from screener.pipeline.types import Flag


class RecomputeReport(BaseModel):
    matches: bool
    differences: list[str]


def recompute(run: RunResult, loaded: LoadedConfig) -> RecomputeReport:
    if run.config_hash != loaded.hash:
        return RecomputeReport(matches=False, differences=[f"config hash {loaded.hash[:12]} is not the run's {run.config_hash[:12]}"])
    score_node = run.node("score")
    redact_node = run.node("redact")
    parse_node = run.node("parse")
    if score_node is None or redact_node is None or parse_node is None or run.aggregate is None:
        return RecomputeReport(matches=True, differences=[])
    redacted_text: str = redact_node.output["redacted_text"]
    offset_map = OffsetMap.model_validate(redact_node.output["offset_map"])
    record = ParsedRecord.model_validate(parse_node.output["record"])
    entries = [offset_map.to_redacted(Span(start=s, end=e)) for s, e in entry_spans(record)]
    entry_ranges = [(s.start, s.end) for s in entries]
    locator = QuoteLocator(redacted_text)
    cfg = loaded.config

    raw_by_key = {(c.request["criterion"], c.request["sample"]): c for c in score_node.llm_calls}
    differences: list[str] = []
    scores = []
    score_flags: list[Flag] = []
    for criterion in loaded.rubric.criteria:
        samples = []
        for i in range(cfg.score.samples):
            call = raw_by_key.get((criterion.id, i))
            resp = response_from_raw(call.model, call.raw_response, call.latency_ms) if call else None
            stored = next(s for s in next(x for x in run.scores if x.criterion == criterion.id).samples if s.index == i)
            samples.append(evaluate_sample(i, resp, None if call else stored.invalid_reason, locator, entry_ranges))
        score, flags = summarize(criterion.id, samples, cfg.score)
        scores.append(score)
        score_flags.extend(flags)
        stored_score = next(x for x in run.scores if x.criterion == criterion.id)
        if score.model_dump() != stored_score.model_dump():
            differences.append(f"criterion {criterion.id}: stored level {stored_score.final_level}, recomputed {score.final_level}")

    upstream = [f for f in run.flags if f.node in ("intake", "parse", "redact")]
    aggregate, _ = run_aggregate(scores, [*upstream, *score_flags], loaded.rubric, cfg.aggregate, cfg.score)
    if aggregate.model_dump() != run.aggregate.model_dump():
        differences.append(f"aggregate: stored {run.aggregate.tier}/{run.aggregate.weighted_score}, recomputed {aggregate.tier}/{aggregate.weighted_score}")
    return RecomputeReport(matches=not differences, differences=differences)
