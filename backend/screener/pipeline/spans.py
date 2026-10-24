"""Locating model-returned quotes in source text (principle 8).

Models return verbatim substrings; this module finds where they are. Matching is
exact or whitespace/typography-normalized, never fuzzy: a quote that doesn't
appear in the source doesn't resolve.
"""

from __future__ import annotations

import unicodedata

from pydantic import BaseModel

# Typographic variants a model commonly swaps when quoting.
_CHAR_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", " ": " ",
}


class Span(BaseModel):
    start: int
    end: int

    def slice(self, text: str) -> str:
        return text[self.start : self.end]


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Normalize text and return, for each normalized char, its source index.

    Collapses whitespace runs to one space, maps typographic quotes and dashes,
    and applies NFKC per character.
    """
    out: list[str] = []
    index: list[int] = []
    in_space = False
    for i, ch in enumerate(text):
        ch = _CHAR_MAP.get(ch, ch)
        for c in unicodedata.normalize("NFKC", ch):
            if c.isspace():
                if in_space or not out:
                    continue
                in_space = True
                out.append(" ")
                index.append(i)
            else:
                in_space = False
                out.append(c)
                index.append(i)
    if out and out[-1] == " ":
        out.pop()
        index.pop()
    return "".join(out), index


def normalize(text: str) -> str:
    return normalize_with_map(text)[0]


class QuoteLocator:
    """Finds quotes in one source text. Build once per text; reuse for many quotes."""

    def __init__(self, source: str):
        self.source = source
        self._norm, self._map = normalize_with_map(source)

    def find(self, quote: str, start_hint: int = 0) -> Span | None:
        """Return the span of `quote` in the source, or None.

        Prefers an exact match at or after `start_hint`, then anywhere; then the
        same for the normalized form.
        """
        if not quote or not quote.strip():
            return None
        q = quote.strip()
        for begin in (start_hint, 0):
            pos = self.source.find(q, begin)
            if pos >= 0:
                return Span(start=pos, end=pos + len(q))
        nq = normalize(q)
        if not nq:
            return None
        norm_hint = next((k for k, src in enumerate(self._map) if src >= start_hint), len(self._map))
        for begin in (norm_hint, 0):
            pos = self._norm.find(nq, begin)
            if pos >= 0:
                start = self._map[pos]
                end = self._map[pos + len(nq) - 1] + 1
                return Span(start=start, end=end)
        return None
