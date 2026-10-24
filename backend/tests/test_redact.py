from screener.config import RedactConfig, TermList
from screener.pipeline.parse import resolve
from screener.pipeline.redact import run_redact
from screener.pipeline.spans import Span

TEXT = (
    "Ada Lovelace\nada.lovelace@example.com | (555) 010-1234 | Austin, TX\nSUMMARY\nShe led the payments team. Ms. Lovelace mentored her peers.\n"
    "EXPERIENCE\nEngineer — Quarry Labs | Jan 2020 – Present\n• Built REST APIs in Go.\nEDUCATION\nB.S. in CS — Lakeview State University, Lakeview, USA | 2012 – 2016\n"
    "ACTIVITIES\nMember, Society of Women Engineers\nLINKS\ngithub.com/adalovelace"
)
RAW = {
    "contact": {"name": "Ada Lovelace", "email": "ada.lovelace@example.com", "phone": "(555) 010-1234", "location": "Austin, TX"},
    "experience": [{"title": "Engineer", "org": "Quarry Labs", "start": "Jan 2020", "end": "Present", "description": "Built REST APIs in Go."}],
    "education": [{"institution": "Lakeview State University", "degree": "B.S.", "field": "CS", "start": "2012", "end": "2016"}],
    "skills": [],
    "certifications": [],
    "links": ["github.com/adalovelace"],
}
TERMS = {"affinity_orgs": TermList(name="affinity_orgs", terms=["Society of Women Engineers"])}


def _redact(config=None, terms=TERMS):
    record, unresolved = resolve(RAW, TEXT)
    assert unresolved == []
    return run_redact(TEXT, record, config or RedactConfig(free_text={"term_lists": list(terms)}), terms)


def test_default_fields_removed():
    result, rmap, node = _redact()
    out = result.redacted_text
    for leaked in ("Ada", "Lovelace", "ada.lovelace@example.com", "010-1234", "Austin", "github.com"):
        assert leaked not in out
    assert "[NAME]" in out and "[EMAIL]" in out and "[PHONE]" in out and "[LOCATION]" in out and "[LINK]" in out
    assert "Ms." not in out
    # The redaction map is returned separately and never inside the node output.
    assert any(r.original == "Ada Lovelace" for r in rmap.redactions)
    assert "Ada Lovelace" not in str(node.output)


def test_pronouns_and_term_lists():
    out = _redact()[0].redacted_text
    assert "They led the payments team" in out
    assert "mentored their peers" in out
    assert "Society of Women Engineers" not in out and "[ORG]" in out


def test_school_names_only_when_configured():
    assert "Lakeview State University" in _redact()[0].redacted_text
    cfg = RedactConfig(optional=["school_names", "graduation_years"])
    out = _redact(cfg, {})[0].redacted_text
    assert "Lakeview State University" not in out and "[SCHOOL]" in out and "[YEAR]" in out


def test_offset_map_round_trips_unredacted_text():
    result, _, _ = _redact()
    r = result.redacted_text
    start = r.index("Built REST APIs in Go.")
    vis = result.offset_map.to_visible(Span(start=start, end=start + len("Built REST APIs in Go.")))
    assert TEXT[vis.start : vis.end] == "Built REST APIs in Go."
    back = result.offset_map.to_redacted(vis)
    assert r[back.start : back.end] == "Built REST APIs in Go."


def test_offset_map_covers_placeholders_with_their_source():
    result, _, _ = _redact()
    r = result.redacted_text
    start = r.index("[NAME]")
    vis = result.offset_map.to_visible(Span(start=start, end=start + 6))
    assert TEXT[vis.start : vis.end] == "Ada Lovelace"
