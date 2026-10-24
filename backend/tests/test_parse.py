from screener.config import ParseConfig
from screener.pipeline.parse import resolve, run_parse
from screener.pipeline.types import FlagType

TEXT = "Ada Lovelace\nada@example.com | (555) 010-1234 | Austin, TX\nEXPERIENCE\nEngineer — Quarry Labs | Jan 2020 – Present\n• Built REST APIs in Go.\nSKILLS\nGo, SQL"

GOOD = {
    "contact": {"name": "Ada Lovelace", "email": "ada@example.com", "phone": "(555) 010-1234", "location": "Austin, TX"},
    "experience": [{"title": "Engineer", "org": "Quarry Labs", "start": "Jan 2020", "end": "Present", "description": "Built REST APIs in Go."}],
    "education": [],
    "skills": ["Go", "SQL"],
    "certifications": [],
    "links": [],
}


def _provider(client):
    return lambda model: client


def test_resolve_computes_spans_in_code():
    record, unresolved = resolve(GOOD, TEXT)
    assert unresolved == []
    assert TEXT[record.contact.name.start : record.contact.name.end] == "Ada Lovelace"
    d = record.experience[0].description
    assert TEXT[d.start : d.end] == "Built REST APIs in Go."


def test_paraphrased_value_does_not_resolve():
    bad = {**GOOD, "experience": [{**GOOD["experience"][0], "description": "Developed REST APIs using Go"}]}
    _, unresolved = resolve(bad, TEXT)
    assert unresolved == ["experience[0].description"]


def test_retry_then_success(scripted):
    answers = [{**GOOD, "skills": ["Rust"]}, GOOD]
    client = scripted(lambda req: answers.pop(0))
    result, node = run_parse(TEXT, ParseConfig(model="test", retries=1), _provider(client))
    assert result.ok and result.attempts == 2
    assert "not verbatim" in client.requests[1].user_blocks[1].text
    assert len(node.llm_calls) == 2
    assert node.flags == []


def test_retries_exhausted_flags_parse_failure(scripted):
    client = scripted(lambda req: {**GOOD, "skills": ["Rust"]})
    result, node = run_parse(TEXT, ParseConfig(model="test", retries=1), _provider(client))
    assert not result.ok
    assert [f.type for f in node.flags] == [FlagType.PARSE_FAILED]
    assert result.record.contact.name is not None  # resolved fields are kept


def test_refusal_is_flagged(scripted):
    client = scripted(lambda req: "", stop_reason="refusal")
    _, node = run_parse(TEXT, ParseConfig(model="test"), _provider(client))
    assert [f.type for f in node.flags] == [FlagType.MODEL_REFUSAL]


def test_resume_is_in_user_turn_not_system(scripted):
    client = scripted(lambda req: GOOD)
    run_parse(TEXT, ParseConfig(model="test"), _provider(client))
    req = client.requests[0]
    assert TEXT not in req.system
    assert req.user_blocks[0].text.startswith("<resume>\n") and TEXT in req.user_blocks[0].text
