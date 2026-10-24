import pymupdf
import pytest

from screener.config import IntakeConfig
from screener.pipeline.intake import run_intake
from screener.pipeline.types import FlagType
from screener.synthetic.fixtures import HIDDEN_KINDS
from screener.synthetic.render import STUFFING


def _flags(result):
    return {f.type for f in result.flags}


def test_clean_resume_has_no_hidden_text(fixture_files):
    result, pdf = run_intake(fixture_files["clean_strong.pdf"], "r.pdf", IntakeConfig())
    assert result.hidden_spans == []
    assert result.flags == []
    assert "EXPERIENCE" in result.visible_text
    assert pdf == fixture_files["clean_strong.pdf"]
    assert result.visible_spans and all(s.page == 0 or s.page > 0 for s in result.visible_spans)


@pytest.mark.parametrize("kind", HIDDEN_KINDS)
def test_every_hidden_kind_is_flagged_and_kept_out_of_visible_text(fixture_files, kind):
    result, _ = run_intake(fixture_files[f"hidden_{kind}.pdf"], "r.pdf", IntakeConfig())
    assert FlagType.HIDDEN_TEXT in _flags(result)
    assert any(STUFFING[:20] in h.text for h in result.hidden_spans)
    assert "Kubernetes Kafka distributed" not in result.visible_text
    flag = next(f for f in result.flags if f.type == FlagType.HIDDEN_TEXT)
    assert "not used in scoring" in flag.detail  # neutral wording, never "suspicious"


def test_light_text_on_dark_background_is_visible():
    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_rect(pymupdf.Rect(50, 50, 400, 90), color=(0, 0, 0.4), fill=(0, 0, 0.4))
    page.insert_text((60, 75), "Senior Engineer", fontsize=12, color=(1, 1, 1))
    result, _ = run_intake(doc.tobytes(), "r.pdf", IntakeConfig())
    assert result.hidden_spans == []
    assert "Senior Engineer" in result.visible_text


def test_image_only_pdf_is_flagged_for_manual_review(fixture_files):
    result, _ = run_intake(fixture_files["image_only.pdf"], "r.pdf", IntakeConfig())
    assert FlagType.NO_EXTRACTABLE_TEXT in _flags(result)
    assert result.visible_text == ""


def test_unsupported_file_never_raises():
    result, pdf = run_intake(b"not a resume", "resume.txt", IntakeConfig())
    assert FlagType.UNSUPPORTED_FILE in _flags(result)
    assert pdf is None


def test_corrupt_pdf_never_raises():
    result, _ = run_intake(b"%PDF-1.7 garbage", "r.pdf", IntakeConfig())
    assert FlagType.UNSUPPORTED_FILE in _flags(result)


def test_docx_hidden_runs_are_flagged(fixture_files):
    result, _ = run_intake(fixture_files["hidden_runs.docx"], "r.docx", IntakeConfig())
    reasons = {r for h in result.hidden_spans for r in h.reasons}
    assert "docx_vanish" in reasons
    assert FlagType.HIDDEN_TEXT in _flags(result)
    assert "Kubernetes Kafka distributed" not in result.visible_text


def test_locate_maps_visible_offsets_to_pages(fixture_files):
    result, _ = run_intake(fixture_files["clean_strong.pdf"], "r.pdf", IntakeConfig())
    start = result.visible_text.index("EXPERIENCE")
    spans = result.locate(start, start + len("EXPERIENCE"))
    assert spans and spans[0].page == 0
