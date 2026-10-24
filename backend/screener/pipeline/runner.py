"""End-to-end run: intake -> parse -> redact -> score -> aggregate.

A run stops early, with status `manual_review`, when continuing would score text
we can't trust or can't redact: no extractable text, an unsupported file, a
parse that failed outright, or a name redaction that couldn't be located. Those
candidates go to a human with no suggested tier. Nothing here rejects anyone.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from screener.config import LoadedConfig
from screener.llm import ClientProvider
from screener.pipeline.aggregate import AggregateResult, run_aggregate
from screener.pipeline.intake import IntakeResult, run_intake
from screener.pipeline.parse import ParseResult, entry_spans, run_parse
from screener.pipeline.redact import RedactionMap, RedactResult, run_redact
from screener.pipeline.score import CriterionScore, run_score
from screener.pipeline.spans import Span
from screener.pipeline.types import Flag, FlagType, NodeOutput


class RunStatus(StrEnum):
    COMPLETED = "completed"
    MANUAL_REVIEW = "manual_review"


class RunResult(BaseModel):
    config_hash: str
    status: RunStatus
    stop_reason: str | None = None
    nodes: list[NodeOutput] = Field(default_factory=list)
    intake: IntakeResult
    parse: ParseResult | None = None
    redact: RedactResult | None = None
    scores: list[CriterionScore] = Field(default_factory=list)
    aggregate: AggregateResult | None = None
    flags: list[Flag] = Field(default_factory=list)

    def node(self, name: str) -> NodeOutput | None:
        return next((n for n in self.nodes if n.node == name), None)


class RunArtifacts(BaseModel):
    """Outputs kept out of the trace: the restricted redaction map and the renderable PDF."""

    redaction_map: RedactionMap | None = None
    render_pdf: bytes | None = None


_STOP_INTAKE = {FlagType.NO_EXTRACTABLE_TEXT, FlagType.UNSUPPORTED_FILE}


def run_pipeline(data: bytes, filename: str, loaded: LoadedConfig, provider: ClientProvider, repeat: int = 0) -> tuple[RunResult, RunArtifacts]:
    cfg = loaded.config
    intake, render_pdf = run_intake(data, filename, cfg.intake)
    nodes = [NodeOutput(node="intake", input_hash=intake.sha256, output=intake.model_dump(mode="json"), config=cfg.intake.model_dump(mode="json"), flags=intake.flags)]
    flags = list(intake.flags)
    artifacts = RunArtifacts(render_pdf=render_pdf)

    def stop(reason: str, **extra) -> tuple[RunResult, RunArtifacts]:
        return RunResult(config_hash=loaded.hash, status=RunStatus.MANUAL_REVIEW, stop_reason=reason, nodes=nodes, intake=intake, flags=flags, **extra), artifacts

    if not intake.visible_text.strip() or any(f.type in _STOP_INTAKE and "pages without" not in f.detail for f in intake.flags):
        return stop("no usable visible text")

    parse, parse_node = run_parse(intake.visible_text, cfg.parse, provider)
    nodes.append(parse_node)
    flags.extend(parse_node.flags)
    if any(f.type in (FlagType.MODEL_ERROR, FlagType.MODEL_REFUSAL) for f in parse_node.flags) or parse.unresolved == ["<invalid json>"]:
        return stop("parse produced no usable record", parse=parse)
    if "name" in cfg.redact.fields and parse.record.contact.name is None:
        flags.append(Flag(node="redact", type=FlagType.PARSE_FAILED, detail="candidate name could not be located, so it can't be redacted; not scored"))
        return stop("name not located for redaction", parse=parse)

    redact, redaction_map, redact_node = run_redact(intake.visible_text, parse.record, cfg.redact, loaded.term_lists)
    nodes.append(redact_node)
    artifacts.redaction_map = redaction_map

    entries = [redact.offset_map.to_redacted(Span(start=s, end=e)) for s, e in entry_spans(parse.record)]
    scores, score_node = run_score(redact.redacted_text, [(s.start, s.end) for s in entries], loaded.rubric, cfg.score, provider, repeat)
    nodes.append(score_node)
    flags.extend(score_node.flags)

    aggregate, agg_node = run_aggregate(scores, flags, loaded.rubric, cfg.aggregate, cfg.score)
    nodes.append(agg_node)
    flags.extend(agg_node.flags)

    return (
        RunResult(config_hash=loaded.hash, status=RunStatus.COMPLETED, nodes=nodes, intake=intake, parse=parse, redact=redact, scores=scores, aggregate=aggregate, flags=flags),
        artifacts,
    )


class EvidenceLocation(BaseModel):
    criterion: str
    quote: str
    visible_span: Span | None
    pages: list[dict]


def evidence_locations(run: RunResult) -> list[EvidenceLocation]:
    """Map each final-score evidence quote back to the original document for highlighting."""
    out: list[EvidenceLocation] = []
    if run.redact is None:
        return out
    for score in run.scores:
        for ev in score.evidence:
            if ev.redacted_span is None:
                out.append(EvidenceLocation(criterion=score.criterion, quote=ev.quote, visible_span=None, pages=[]))
                continue
            vis = run.redact.offset_map.to_visible(ev.redacted_span)
            pages = [{"page": s.page, "bbox": s.bbox} for s in run.intake.locate(vis.start, vis.end)]
            out.append(EvidenceLocation(criterion=score.criterion, quote=ev.quote, visible_span=vis, pages=pages))
    return out
