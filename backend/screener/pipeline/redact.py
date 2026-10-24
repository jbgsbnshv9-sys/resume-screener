"""Node 3: redact (deterministic).

Replaces configured fields in the visible text with bracketed placeholders, and
runs a best-effort free-text pass (pronouns, term lists). Produces:

- the redacted text that scoring sees;
- an `OffsetMap` between redacted and visible coordinates, so evidence verified
  against redacted text can be highlighted in the original document;
- a `RedactionMap` of what was removed, stored separately under tighter access.
"""

from __future__ import annotations

import re
import time

from pydantic import BaseModel, Field

from screener.config import RedactConfig, TermList
from screener.hashing import hash_value
from screener.pipeline.parse import ParsedRecord
from screener.pipeline.spans import Span
from screener.pipeline.types import NodeOutput

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
_URL = re.compile(r"(?:https?://|www\.)\S+|\b(?:linkedin\.com|github\.com|gitlab\.com)/\S+", re.IGNORECASE)
_HONORIFIC = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Mx)\.?\s+(?=[A-Z\[])")

_PRONOUNS = {
    "he": "they", "she": "they", "him": "them", "his": "their", "her": "their",
    "hers": "theirs", "himself": "themself", "herself": "themself",
}
_PRONOUN_RE = re.compile(r"\b(" + "|".join(_PRONOUNS) + r")\b", re.IGNORECASE)


class Segment(BaseModel):
    """A stretch of redacted text and the visible text it came from."""

    r_start: int
    r_end: int
    v_start: int
    v_end: int
    replaced: bool


class OffsetMap(BaseModel):
    segments: list[Segment]

    def _map(self, pos: int, *, to_visible: bool, is_end: bool) -> int:
        src_s, src_e, dst_s, dst_e = ("r_start", "r_end", "v_start", "v_end") if to_visible else ("v_start", "v_end", "r_start", "r_end")
        for seg in self.segments:
            s, e = getattr(seg, src_s), getattr(seg, src_e)
            inside = s <= pos < e if not is_end else s < pos <= e
            if inside:
                if seg.replaced:
                    return getattr(seg, dst_e) if is_end else getattr(seg, dst_s)
                return getattr(seg, dst_s) + (pos - s)
        last = self.segments[-1] if self.segments else None
        return getattr(last, dst_e) if last else pos

    def to_visible(self, span: Span) -> Span:
        return Span(start=self._map(span.start, to_visible=True, is_end=False), end=self._map(span.end, to_visible=True, is_end=True))

    def to_redacted(self, span: Span) -> Span:
        return Span(start=self._map(span.start, to_visible=False, is_end=False), end=self._map(span.end, to_visible=False, is_end=True))


class Redaction(BaseModel):
    category: str
    placeholder: str
    original: str
    v_start: int
    v_end: int


class RedactionMap(BaseModel):
    """What was removed. Restricted: never shown alongside scores to roles without access."""

    redactions: list[Redaction]


class RedactResult(BaseModel):
    redacted_text: str
    offset_map: OffsetMap
    categories: dict[str, int] = Field(default_factory=dict)
    photo_note: str = "images are not extracted, so photos never reach scoring"


def _word_occurrences(text: str, phrase: str, *, ignore_case: bool) -> list[tuple[int, int]]:
    if not phrase.strip():
        return []
    pattern = re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", re.IGNORECASE if ignore_case else 0)
    return [(m.start(), m.end()) for m in pattern.finditer(text)]


def _case_like(src: str, repl: str) -> str:
    if src.isupper() and len(src) > 1:
        return repl.upper()
    if src[:1].isupper():
        return repl[:1].upper() + repl[1:]
    return repl


def collect_redactions(visible_text: str, record: ParsedRecord, config: RedactConfig, term_lists: dict[str, TermList]) -> list[Redaction]:
    found: list[Redaction] = []

    def add(category: str, placeholder: str, start: int, end: int) -> None:
        if end > start:
            found.append(Redaction(category=category, placeholder=placeholder, original=visible_text[start:end], v_start=start, v_end=end))

    c = record.contact
    if "name" in config.fields and c.name is not None:
        add("name", "[NAME]", c.name.start, c.name.end)
        for token in re.findall(r"[^\W\d_][\w'-]*", c.name.text):
            if len(token) >= 2:
                for s, e in _word_occurrences(visible_text, token, ignore_case=False):
                    add("name", "[NAME]", s, e)
        for m in _HONORIFIC.finditer(visible_text):
            add("name", "", m.start(), m.end())
    if "contact" in config.fields:
        for f, ph in ((c.email, "[EMAIL]"), (c.phone, "[PHONE]")):
            if f is not None:
                add("contact", ph, f.start, f.end)
        for m in _EMAIL.finditer(visible_text):
            add("contact", "[EMAIL]", m.start(), m.end())
        for m in _PHONE.finditer(visible_text):
            add("contact", "[PHONE]", m.start(), m.end())
    if "address" in config.fields and c.location is not None:
        add("address", "[LOCATION]", c.location.start, c.location.end)
    if "links" in config.fields:
        for f in record.links:
            add("links", "[LINK]", f.start, f.end)
        for m in _URL.finditer(visible_text):
            add("links", "[LINK]", m.start(), m.end())
    if "school_names" in config.optional:
        for ed in record.education:
            if ed.institution is not None:
                for s, e in _word_occurrences(visible_text, ed.institution.text, ignore_case=True):
                    add("school_names", "[SCHOOL]", s, e)
    if "graduation_years" in config.optional:
        for ed in record.education:
            for f in (ed.start, ed.end):
                if f is not None:
                    add("graduation_years", "[YEAR]", f.start, f.end)
    if "dates" in config.optional:
        for item in [*record.experience, *record.education]:
            for f in (item.start, item.end):
                if f is not None:
                    add("dates", "[DATE]", f.start, f.end)
    if config.free_text.neutralize_pronouns:
        for m in _PRONOUN_RE.finditer(visible_text):
            add("pronoun", _case_like(m.group(0), _PRONOUNS[m.group(0).lower()]), m.start(), m.end())
    for name, tl in term_lists.items():
        for term in tl.terms:
            for s, e in _word_occurrences(visible_text, term, ignore_case=True):
                add(f"term_list:{name}", "[ORG]", s, e)
    return _merge(found)


def _merge(found: list[Redaction]) -> list[Redaction]:
    """Drop redactions overlapping an earlier/longer one so replacements never collide."""
    found.sort(key=lambda r: (r.v_start, -(r.v_end - r.v_start)))
    merged: list[Redaction] = []
    for r in found:
        if merged and r.v_start < merged[-1].v_end:
            if r.v_end > merged[-1].v_end:
                prev = merged[-1]
                merged[-1] = prev.model_copy(update={"v_end": r.v_end, "original": prev.original + r.original[prev.v_end - r.v_start :]})
            continue
        merged.append(r)
    return merged


def apply_redactions(visible_text: str, redactions: list[Redaction]) -> tuple[str, OffsetMap]:
    parts: list[str] = []
    segments: list[Segment] = []
    v = 0
    r = 0
    for red in redactions:
        if red.v_start > v:
            chunk = visible_text[v : red.v_start]
            parts.append(chunk)
            segments.append(Segment(r_start=r, r_end=r + len(chunk), v_start=v, v_end=red.v_start, replaced=False))
            r += len(chunk)
        parts.append(red.placeholder)
        segments.append(Segment(r_start=r, r_end=r + len(red.placeholder), v_start=red.v_start, v_end=red.v_end, replaced=True))
        r += len(red.placeholder)
        v = red.v_end
    if v < len(visible_text):
        chunk = visible_text[v:]
        parts.append(chunk)
        segments.append(Segment(r_start=r, r_end=r + len(chunk), v_start=v, v_end=len(visible_text), replaced=False))
    return "".join(parts), OffsetMap(segments=segments)


def run_redact(visible_text: str, record: ParsedRecord, config: RedactConfig, term_lists: dict[str, TermList]) -> tuple[RedactResult, RedactionMap, NodeOutput]:
    started = time.monotonic()
    redactions = collect_redactions(visible_text, record, config, term_lists)
    redacted_text, offset_map = apply_redactions(visible_text, redactions)
    categories: dict[str, int] = {}
    for red in redactions:
        categories[red.category] = categories.get(red.category, 0) + 1
    result = RedactResult(redacted_text=redacted_text, offset_map=offset_map, categories=categories)
    node = NodeOutput(
        node="redact",
        input_hash=hash_value({"visible": visible_text, "record": record.model_dump(mode="json")}),
        output=result.model_dump(mode="json"),
        config=config.model_dump(mode="json"),
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return result, RedactionMap(redactions=redactions), node
