"""Node 2: parse (LLM).

The model transcribes visible text into a fixed schema, quoting every value
verbatim. Code then locates each quote in the visible text (principle 8). A
value that doesn't resolve fails validation; after the configured retries the
candidate is flagged for human review.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from screener.config import ParseConfig
from screener.hashing import sha256_text
from screener.llm import ClientProvider, LLMError, LLMRequest, UserBlock
from screener.pipeline.spans import QuoteLocator
from screener.pipeline.types import Flag, FlagType, LLMCallRecord, NodeOutput, TokenUsage
from screener.prompts import load_prompt


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": _STR}

PARSE_SCHEMA: dict[str, Any] = _obj(
    {
        "contact": _obj({"name": _STR, "email": _STR, "phone": _STR, "location": _STR}),
        "experience": {"type": "array", "items": _obj({"title": _STR, "org": _STR, "start": _STR, "end": _STR, "description": _STR})},
        "education": {"type": "array", "items": _obj({"institution": _STR, "degree": _STR, "field": _STR, "start": _STR, "end": _STR})},
        "skills": _STR_LIST,
        "certifications": _STR_LIST,
        "links": _STR_LIST,
    }
)


class Field_(BaseModel):
    """A value and the span it occupies in the visible text."""

    text: str
    start: int
    end: int


class Contact(BaseModel):
    name: Field_ | None = None
    email: Field_ | None = None
    phone: Field_ | None = None
    location: Field_ | None = None


class Experience(BaseModel):
    title: Field_ | None = None
    org: Field_ | None = None
    start: Field_ | None = None
    end: Field_ | None = None
    description: Field_ | None = None


class Education(BaseModel):
    institution: Field_ | None = None
    degree: Field_ | None = None
    field: Field_ | None = None
    start: Field_ | None = None
    end: Field_ | None = None


class ParsedRecord(BaseModel):
    contact: Contact = Field(default_factory=Contact)
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[Field_] = Field(default_factory=list)
    certifications: list[Field_] = Field(default_factory=list)
    links: list[Field_] = Field(default_factory=list)


class ParseResult(BaseModel):
    record: ParsedRecord
    ok: bool
    unresolved: list[str]
    attempts: int


def resolve(raw: dict[str, Any], visible_text: str) -> tuple[ParsedRecord, list[str]]:
    """Turn the model's quoted values into spans. Returns the record and unresolved paths."""
    locator = QuoteLocator(visible_text)
    unresolved: list[str] = []
    cursor = 0

    def field(path: str, value: str) -> Field_ | None:
        nonlocal cursor
        if not value or not value.strip():
            return None
        span = locator.find(value, cursor)
        if span is None:
            unresolved.append(path)
            return None
        cursor = span.start
        return Field_(text=visible_text[span.start : span.end], start=span.start, end=span.end)

    c = raw.get("contact", {})
    contact = Contact(**{k: field(f"contact.{k}", c.get(k, "")) for k in ("name", "email", "phone", "location")})
    experience = [
        Experience(**{k: field(f"experience[{i}].{k}", e.get(k, "")) for k in ("title", "org", "start", "end", "description")})
        for i, e in enumerate(raw.get("experience", []))
    ]
    education = [
        Education(**{k: field(f"education[{i}].{k}", e.get(k, "")) for k in ("institution", "degree", "field", "start", "end")})
        for i, e in enumerate(raw.get("education", []))
    ]

    def flist(name: str) -> list[Field_]:
        out = []
        for i, v in enumerate(raw.get(name, [])):
            f = field(f"{name}[{i}]", v)
            if f is not None:
                out.append(f)
        return out

    record = ParsedRecord(contact=contact, experience=experience, education=education, skills=flist("skills"), certifications=flist("certifications"), links=flist("links"))
    return record, unresolved


def build_request(visible_text: str, config: ParseConfig, retry_note: str = "", attempt: int = 0) -> LLMRequest:
    system = load_prompt("parse_system")
    block = load_prompt("resume_block")
    user = load_prompt("parse_user")
    return LLMRequest(
        task="parse",
        model=config.model,
        system=system.text,
        user_blocks=[UserBlock(text=block.render(resume=visible_text)), UserBlock(text=user.render(retry_note=retry_note))],
        output_schema=PARSE_SCHEMA,
        max_tokens=config.max_tokens,
        effort=config.effort,
        prompt_hash=sha256_text(system.hash + block.hash + user.hash),
        sample_index=attempt,
    )


def run_parse(visible_text: str, config: ParseConfig, provider: ClientProvider) -> tuple[ParseResult, NodeOutput]:
    started = time.monotonic()
    client = provider(config.model)
    calls: list[LLMCallRecord] = []
    flags: list[Flag] = []
    usage = TokenUsage()
    retry_note = ""
    record = ParsedRecord()
    unresolved: list[str] = ["<no response>"]
    attempts = 0
    for attempt in range(config.retries + 1):
        attempts = attempt + 1
        request = build_request(visible_text, config, retry_note, attempt)
        try:
            response = client.complete(request)
        except LLMError as e:
            flags.append(Flag(node="parse", type=FlagType.MODEL_ERROR, detail=f"parse call failed ({e.kind})"))
            break
        calls.append(LLMCallRecord(model=response.model, prompt_hash=request.prompt_hash, request=request.api_params(), raw_response=response.raw, stop_reason=response.stop_reason, usage=response.usage, latency_ms=response.latency_ms))
        usage = usage + response.usage
        if response.stop_reason == "refusal":
            flags.append(Flag(node="parse", type=FlagType.MODEL_REFUSAL, detail="model declined to parse this document"))
            break
        if response.parsed is None:
            unresolved = ["<invalid json>"]
            retry_note = "\n\nYour previous answer was not valid JSON for the schema. Answer with JSON only."
            continue
        record, unresolved = resolve(response.parsed, visible_text)
        if not unresolved:
            break
        retry_note = (
            "\n\nIn your previous answer these fields were not verbatim copies of text in the resume: "
            + ", ".join(unresolved[:20])
            + ". Copy them exactly as they appear, or use an empty string."
        )
    ok = not unresolved
    if not ok and not any(f.type in (FlagType.MODEL_ERROR, FlagType.MODEL_REFUSAL) for f in flags):
        flags.append(Flag(node="parse", type=FlagType.PARSE_FAILED, detail=f"{len(unresolved)} field(s) did not resolve to the document after {attempts} attempt(s): {', '.join(unresolved[:5])}"))
    result = ParseResult(record=record, ok=ok, unresolved=unresolved, attempts=attempts)
    node = NodeOutput(
        node="parse",
        input_hash=sha256_text(visible_text),
        output=result.model_dump(mode="json"),
        config=config.model_dump(mode="json"),
        flags=flags,
        llm_calls=calls,
        usage=usage,
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return result, node


def entry_spans(record: ParsedRecord) -> list[tuple[int, int]]:
    """Visible-text ranges of experience entries and certifications (for the level >= 2 evidence rule)."""
    spans: list[tuple[int, int]] = []
    for e in record.experience:
        parts = [f for f in (e.title, e.org, e.description) if f is not None]
        if parts:
            spans.append((min(f.start for f in parts), max(f.end for f in parts)))
    spans.extend((c.start, c.end) for c in record.certifications)
    return spans

