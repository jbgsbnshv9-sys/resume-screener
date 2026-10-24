"""Node 1: intake (deterministic).

Extracts text with per-span style and position, renders each page, and flags
spans a human reader wouldn't see. Hidden text is kept for the trace but never
reaches scoring. Intake never rejects; it only flags.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

import pymupdf
from pydantic import BaseModel, Field

from screener.config import IntakeConfig
from screener.hashing import sha256_bytes
from screener.pipeline.types import Flag, FlagType

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# A rendered crop counts as "ink" where a pixel differs from the crop's background by more than this (0-255).
_INK_DELTA = 40
_METADATA_FIELDS = ("keywords", "subject")


class TextSpan(BaseModel):
    """Where a stretch of visible text sits in the original document."""

    start: int
    end: int
    page: int
    bbox: tuple[float, float, float, float]


class HiddenSpan(BaseModel):
    text: str
    reasons: list[str]
    source: str  # pdf | docx | metadata
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None


class IntakeResult(BaseModel):
    sha256: str
    mime: str
    page_count: int = 0
    visible_text: str = ""
    full_text: str = ""
    visible_spans: list[TextSpan] = Field(default_factory=list)
    hidden_spans: list[HiddenSpan] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    converted_from_docx: bool = False

    def locate(self, start: int, end: int) -> list[TextSpan]:
        """Document locations covering visible-text range [start, end)."""
        return [s for s in self.visible_spans if s.start < end and s.end > start]


def detect_mime(filename: str, data: bytes) -> str:
    if data[:5] == b"%PDF-":
        return PDF_MIME
    if data[:2] == b"PK" and filename.lower().endswith(".docx"):
        return DOCX_MIME
    return "application/octet-stream"


def run_intake(data: bytes, filename: str, config: IntakeConfig) -> tuple[IntakeResult, bytes | None]:
    """Returns the result and the PDF bytes the viewer should render (None if unavailable)."""
    mime = detect_mime(filename, data)
    sha = sha256_bytes(data)
    if mime == PDF_MIME:
        return _analyze_pdf(data, sha, mime, config, docx_hidden=[]), data
    if mime == DOCX_MIME:
        docx_hidden = _scan_docx(data, config)
        pdf = _docx_to_pdf(data)
        if pdf is None:
            result = _docx_text_only(data, sha, docx_hidden)
            return result, None
        result = _analyze_pdf(pdf, sha, mime, config, docx_hidden=docx_hidden)
        result.converted_from_docx = True
        return result, pdf
    return (
        IntakeResult(
            sha256=sha,
            mime=mime,
            flags=[Flag(node="intake", type=FlagType.UNSUPPORTED_FILE, detail=f"unsupported file type for {filename!r}; review manually")],
        ),
        None,
    )


def _rgb(color_int: int) -> tuple[int, int, int]:
    return (color_int >> 16) & 255, (color_int >> 8) & 255, color_int & 255


def _crop_stats(pix: pymupdf.Pixmap, rect: pymupdf.Rect) -> tuple[tuple[int, int, int] | None, int, int]:
    """Background colour (most common pixel), ink pixel count, and total pixels in rect."""
    x0, y0 = max(int(rect.x0), 0), max(int(rect.y0), 0)
    x1, y1 = min(int(rect.x1 + 0.999), pix.width), min(int(rect.y1 + 0.999), pix.height)
    if x1 <= x0 or y1 <= y0:
        return None, 0, 0
    samples = pix.samples_mv
    n, stride = pix.n, pix.stride
    pixels = []
    for y in range(y0, y1):
        row = y * stride
        for x in range(x0, x1):
            i = row + x * n
            pixels.append((samples[i], samples[i + 1], samples[i + 2]))
    bg = Counter(pixels).most_common(1)[0][0]
    ink = sum(1 for p in pixels if max(abs(p[0] - bg[0]), abs(p[1] - bg[1]), abs(p[2] - bg[2])) > _INK_DELTA)
    return bg, ink, len(pixels)


def _span_hidden_reasons(span: dict, page_rect: pymupdf.Rect, pix: pymupdf.Pixmap, scale: float, config: IntakeConfig) -> list[str]:
    reasons: list[str] = []
    if span["size"] < config.min_font_pt:
        reasons.append("tiny_font")
    bbox = pymupdf.Rect(span["bbox"])
    area = max(bbox.get_area(), 1e-6)
    if (bbox & page_rect).get_area() / area < 0.5:
        reasons.append("off_page")
        return reasons
    if span.get("alpha", 255) == 0:
        reasons.append("zero_opacity")
    bg, ink, total = _crop_stats(pix, bbox * scale)
    if bg is None:
        reasons.append("off_page")
        return reasons
    fg = _rgb(span["color"])
    delta = max(abs(fg[i] - bg[i]) for i in range(3)) / 255
    if delta < config.color_delta_threshold:
        reasons.append("low_contrast")
    # Nothing visibly drawn where the text should be: covered by a shape, transparent, or blended away.
    if total and ink < max(2, int(0.003 * total)):
        reasons.append("not_rendered")
    return reasons


def _analyze_pdf(pdf: bytes, sha: str, mime: str, config: IntakeConfig, docx_hidden: list[HiddenSpan]) -> IntakeResult:
    try:
        doc = pymupdf.open(stream=pdf, filetype="pdf")
    except Exception as e:  # corrupt or encrypted files go to a human, never an exception up the stack
        return IntakeResult(sha256=sha, mime=mime, flags=[Flag(node="intake", type=FlagType.UNSUPPORTED_FILE, detail=f"could not open PDF: {type(e).__name__}")])

    visible_parts: list[str] = []
    full_parts: list[str] = []
    visible_len = 0
    full_len = 0
    visible_spans: list[TextSpan] = []
    hidden: list[HiddenSpan] = list(docx_hidden)
    flags: list[Flag] = []
    empty_pages: list[int] = []

    scale = config.render_dpi / 72
    for page_index, page in enumerate(doc):
        pix = page.get_pixmap(dpi=config.render_dpi, alpha=False, colorspace=pymupdf.csRGB)
        page_rect = page.rect
        # A clip larger than the page keeps off-page spans in the extraction so they can be flagged.
        clip = pymupdf.Rect(-10_000, -10_000, 10_000, 10_000)
        data = page.get_text("dict", sort=True, clip=clip)
        page_has_text = False
        for block in data["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                vis_line: list[tuple[str, tuple[float, float, float, float]]] = []
                full_line: list[str] = []
                for span in line["spans"]:
                    text = span["text"]
                    if not text:
                        continue
                    full_line.append(text)
                    if not text.strip():
                        vis_line.append((text, tuple(span["bbox"])))
                        continue
                    page_has_text = True
                    reasons = _span_hidden_reasons(span, page_rect, pix, scale, config)
                    if reasons:
                        hidden.append(HiddenSpan(text=text, reasons=reasons, source="pdf", page=page_index, bbox=tuple(span["bbox"])))
                    else:
                        vis_line.append((text, tuple(span["bbox"])))
                if full_line:
                    joined = "".join(full_line)
                    if full_parts:
                        full_parts.append("\n")
                        full_len += 1
                    full_parts.append(joined)
                    full_len += len(joined)
                line_text = "".join(t for t, _ in vis_line)
                if line_text.strip():
                    if visible_parts:
                        visible_parts.append("\n")
                        visible_len += 1
                    offset = visible_len
                    for text, bbox in vis_line:
                        if text.strip():
                            visible_spans.append(TextSpan(start=offset, end=offset + len(text), page=page_index, bbox=bbox))
                        offset += len(text)
                    visible_parts.append(line_text)
                    visible_len += len(line_text)
        if not page_has_text:
            empty_pages.append(page_index)

    for field in _METADATA_FIELDS:
        value = (doc.metadata or {}).get(field) or ""
        if value.strip():
            hidden.append(HiddenSpan(text=value, reasons=["metadata_field"], source="metadata"))

    visible_text = "".join(visible_parts)
    if not visible_text.strip():
        flags.append(Flag(node="intake", type=FlagType.NO_EXTRACTABLE_TEXT, detail="no extractable text (scanned or image-only document); review manually"))
    elif empty_pages:
        flags.append(Flag(node="intake", type=FlagType.NO_EXTRACTABLE_TEXT, detail=f"pages without extractable text: {[p + 1 for p in empty_pages]}"))
    if hidden:
        flags.append(Flag(node="intake", type=FlagType.HIDDEN_TEXT, detail=f"hidden text present ({len(hidden)} spans), not used in scoring"))

    return IntakeResult(
        sha256=sha,
        mime=mime,
        page_count=doc.page_count,
        visible_text=visible_text,
        full_text="".join(full_parts),
        visible_spans=visible_spans,
        hidden_spans=hidden,
        flags=flags,
    )


# --- DOCX ---------------------------------------------------------------------


def _iter_docx_runs(document):
    for paragraph in document.paragraphs:
        yield from paragraph.runs
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    yield from paragraph.runs


def _scan_docx(data: bytes, config: IntakeConfig) -> list[HiddenSpan]:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception:
        return []
    hidden: list[HiddenSpan] = []
    for run in _iter_docx_runs(document):
        if not run.text.strip():
            continue
        reasons = []
        if run.font.hidden:
            reasons.append("docx_vanish")
        rgb = run.font.color.rgb if run.font.color is not None and run.font.color.type is not None else None
        if rgb is not None and min(rgb[0], rgb[1], rgb[2]) >= 250:
            reasons.append("docx_white_text")
        if run.font.size is not None and run.font.size.pt < config.min_font_pt:
            reasons.append("docx_tiny_font")
        if reasons:
            hidden.append(HiddenSpan(text=run.text, reasons=reasons, source="docx"))
    props = document.core_properties
    for value in (props.keywords, props.subject):
        if value and value.strip():
            hidden.append(HiddenSpan(text=value, reasons=["metadata_field"], source="metadata"))
    return hidden


def _docx_to_pdf(data: bytes) -> bytes | None:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "input.docx"
        src.write_bytes(data)
        try:
            subprocess.run(
                [soffice, "--headless", "--norestore", f"-env:UserInstallation=file://{tmp}/profile", "--convert-to", "pdf", "--outdir", tmp, str(src)],
                check=True,
                capture_output=True,
                timeout=120,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        out = Path(tmp) / "input.pdf"
        return out.read_bytes() if out.exists() else None


def _docx_text_only(data: bytes, sha: str, docx_hidden: list[HiddenSpan]) -> IntakeResult:
    """Fallback when DOCX can't be rendered: visible runs only, no positions."""
    import docx

    document = docx.Document(io.BytesIO(data))
    hidden_texts = {h.text for h in docx_hidden if h.source == "docx"}
    lines = []
    for paragraph in document.paragraphs:
        line = "".join(r.text for r in paragraph.runs if r.text not in hidden_texts)
        if line.strip():
            lines.append(line)
    flags = [Flag(node="intake", type=FlagType.UNSUPPORTED_FILE, detail="DOCX could not be rendered; visual hidden-text checks skipped, review manually")]
    if docx_hidden:
        flags.append(Flag(node="intake", type=FlagType.HIDDEN_TEXT, detail=f"hidden text present ({len(docx_hidden)} spans), not used in scoring"))
    text = "\n".join(lines)
    return IntakeResult(sha256=sha, mime=DOCX_MIME, visible_text=text, full_text=text, hidden_spans=docx_hidden, flags=flags)
