"""Node 4: score (LLM).

One call per criterion per sample, so criteria can't influence each other. Every
sample is recorded. Evidence quotes are located in the redacted text by code; a
sample whose evidence doesn't verify is invalid and excluded from the median.

Rules (SPEC.md, node 4):
- level 0 = no supporting evidence; no quote required;
- levels 1-3 need at least one verified quote;
- level >= 2 needs a quote inside an experience entry or certification; a
  skills-list-only match is capped at level 1;
- final level = lower median of valid samples; spread > threshold flags the
  criterion as unstable.
"""

from __future__ import annotations

import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import BaseModel, Field

from screener.config import Criterion, Rubric, ScoreConfig
from screener.hashing import sha256_text
from screener.llm import ClientProvider, LLMClient, LLMError, LLMRequest, LLMResponse, UserBlock
from screener.pipeline.spans import QuoteLocator, Span
from screener.pipeline.types import Flag, FlagType, LLMCallRecord, NodeOutput, TokenUsage
from screener.prompts import load_prompt

SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "level": {"type": "integer", "enum": [0, 1, 2, 3]},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["level", "evidence", "rationale"],
    "additionalProperties": False,
}

MAX_WORKERS = 8


class Evidence(BaseModel):
    quote: str
    verified: bool
    in_entry: bool = False
    redacted_span: Span | None = None


class Sample(BaseModel):
    index: int
    claimed_level: int | None
    level: int | None  # after the entry-evidence cap; None when invalid
    evidence: list[Evidence] = Field(default_factory=list)
    rationale: str = ""
    valid: bool
    invalid_reason: str | None = None
    capped: bool = False
    stop_reason: str | None = None


class CriterionScore(BaseModel):
    criterion: str
    samples: list[Sample]
    final_level: int | None
    verified: bool
    unstable: bool
    evidence: list[Evidence] = Field(default_factory=list)
    rationale: str = ""

    def valid_levels(self) -> list[int]:
        return [s.level for s in self.samples if s.valid and s.level is not None]


def criterion_payload(criterion: Criterion) -> str:
    return json.dumps(
        {
            "name": criterion.name,
            "description": criterion.description,
            "levels": {str(k): {"description": v.description, "example": v.example} for k, v in sorted(criterion.levels.items())},
        },
        indent=2,
        ensure_ascii=False,
    )


def build_request(redacted_text: str, criterion: Criterion, config: ScoreConfig, sample_index: int) -> LLMRequest:
    system = load_prompt("score_system")
    block = load_prompt("resume_block")
    user = load_prompt("score_user")
    return LLMRequest(
        task="score",
        model=config.model,
        system=system.text,
        # Resume first so every criterion call for this candidate shares a cacheable prefix.
        user_blocks=[UserBlock(text=block.render(resume=redacted_text), cache=True), UserBlock(text=user.render(criterion=criterion_payload(criterion)))],
        output_schema=SCORE_SCHEMA,
        max_tokens=config.max_tokens,
        effort=config.effort,
        prompt_hash=sha256_text(system.hash + block.hash + user.hash),
        sample_index=sample_index,
    )


def evaluate_sample(index: int, response: LLMResponse | None, error: str | None, locator: QuoteLocator, entries: list[tuple[int, int]]) -> Sample:
    if error is not None or response is None:
        return Sample(index=index, claimed_level=None, level=None, valid=False, invalid_reason=error or "no_response")
    if response.stop_reason == "refusal":
        return Sample(index=index, claimed_level=None, level=None, valid=False, invalid_reason="refusal", stop_reason=response.stop_reason)
    p = response.parsed
    if not p or not isinstance(p.get("level"), int) or p["level"] not in (0, 1, 2, 3) or not isinstance(p.get("evidence"), list):
        return Sample(index=index, claimed_level=None, level=None, valid=False, invalid_reason="invalid_output", stop_reason=response.stop_reason)
    claimed = p["level"]
    evidence: list[Evidence] = []
    for quote in p["evidence"]:
        if not isinstance(quote, str):
            continue
        span = locator.find(quote)
        in_entry = span is not None and any(s <= span.start and span.end <= e for s, e in entries)
        evidence.append(Evidence(quote=quote, verified=span is not None, in_entry=in_entry, redacted_span=span))
    rationale = str(p.get("rationale", ""))
    base = dict(index=index, claimed_level=claimed, evidence=evidence, rationale=rationale, stop_reason=response.stop_reason)
    if claimed == 0:
        return Sample(level=0, valid=True, **base)
    if not evidence:
        return Sample(level=None, valid=False, invalid_reason="no_evidence", **base)
    if not all(e.verified for e in evidence):
        return Sample(level=None, valid=False, invalid_reason="unverified_quote", **base)
    if claimed >= 2 and not any(e.in_entry for e in evidence):
        return Sample(level=1, valid=True, capped=True, **base)
    return Sample(level=claimed, valid=True, **base)


def summarize(criterion_id: str, samples: list[Sample], config: ScoreConfig) -> tuple[CriterionScore, list[Flag]]:
    flags: list[Flag] = []
    valid = [s for s in samples if s.valid and s.level is not None]
    levels = [s.level for s in valid]
    final = statistics.median_low(levels) if levels else None
    invalid = [s for s in samples if not s.valid]
    if any(s.invalid_reason == "refusal" for s in invalid):
        flags.append(Flag(node="score", type=FlagType.MODEL_REFUSAL, criterion=criterion_id, detail="model declined one or more samples"))
    errored = [s for s in invalid if s.invalid_reason not in ("refusal", "no_evidence", "unverified_quote", "invalid_output")]
    if errored:
        flags.append(Flag(node="score", type=FlagType.MODEL_ERROR, criterion=criterion_id, detail=f"{len(errored)} sample(s) failed: {errored[0].invalid_reason}"))
    unverified = [s for s in invalid if s.invalid_reason in ("no_evidence", "unverified_quote", "invalid_output")]
    if unverified:
        flags.append(Flag(node="score", type=FlagType.UNVERIFIED_EVIDENCE, criterion=criterion_id, detail=f"{len(unverified)} of {len(samples)} sample(s) had evidence that didn't verify ({unverified[0].invalid_reason}); excluded from the median"))
    if final is None:
        flags.append(Flag(node="score", type=FlagType.UNVERIFIED_EVIDENCE, criterion=criterion_id, detail="no valid samples; criterion has no score"))
    unstable = bool(levels) and (max(levels) - min(levels)) > config.instability_threshold
    if unstable:
        flags.append(Flag(node="score", type=FlagType.UNSTABLE_CRITERION, criterion=criterion_id, detail=f"sample levels {levels} spread more than {config.instability_threshold}"))
    chosen = next((s for s in valid if s.level == final), None)
    score = CriterionScore(
        criterion=criterion_id,
        samples=samples,
        final_level=final,
        verified=not invalid,
        unstable=unstable,
        evidence=chosen.evidence if chosen else [],
        rationale=chosen.rationale if chosen else "",
    )
    return score, flags


def run_score(redacted_text: str, entries: list[tuple[int, int]], rubric: Rubric, config: ScoreConfig, provider: ClientProvider, repeat: int = 0) -> tuple[list[CriterionScore], NodeOutput]:
    """`entries` are experience/certification ranges in redacted-text coordinates.

    `repeat` distinguishes whole re-runs of the same candidate (consistency evals),
    so recorded and simulated responses differ across repeats as live ones would.
    """
    started = time.monotonic()
    client = provider(config.model)
    locator = QuoteLocator(redacted_text)
    jobs = [(c, i) for c in rubric.criteria for i in range(config.samples)]
    requests = {(c.id, i): build_request(redacted_text, c, config, repeat * config.samples + i) for c, i in jobs}

    def call(key: tuple[str, int], cl: LLMClient = client) -> tuple[tuple[str, int], LLMResponse | None, str | None]:
        try:
            return key, cl.complete(requests[key]), None
        except LLMError as e:
            return key, None, e.kind

    keys = list(requests)
    # The first call writes the cache for the shared resume prefix; the rest can read it.
    results = [call(keys[0])]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results.extend(pool.map(call, keys[1:]))
    by_key = {k: (resp, err) for k, resp, err in results}

    calls: list[LLMCallRecord] = []
    usage = TokenUsage()
    scores: list[CriterionScore] = []
    flags: list[Flag] = []
    for criterion in rubric.criteria:
        samples = []
        for i in range(config.samples):
            resp, err = by_key[(criterion.id, i)]
            req = requests[(criterion.id, i)]
            if resp is not None:
                calls.append(LLMCallRecord(model=resp.model, prompt_hash=req.prompt_hash, request={"criterion": criterion.id, "sample": i, **req.api_params()}, raw_response=resp.raw, stop_reason=resp.stop_reason, usage=resp.usage, latency_ms=resp.latency_ms))
                usage = usage + resp.usage
            samples.append(evaluate_sample(i, resp, err, locator, entries))
        score, f = summarize(criterion.id, samples, config)
        scores.append(score)
        flags.extend(f)
    node = NodeOutput(
        node="score",
        input_hash=sha256_text(redacted_text),
        output={"scores": [s.model_dump(mode="json") for s in scores]},
        config=config.model_dump(mode="json"),
        flags=flags,
        llm_calls=calls,
        usage=usage,
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return scores, node
