from screener.config import derive_config
from screener.pipeline.recompute import recompute
from screener.pipeline.runner import RunStatus, evidence_locations, run_pipeline
from screener.pipeline.types import FlagType
from tests.conftest import requires_soffice


def test_end_to_end_clean_resume(fixture_files, sim_config, provider):
    run, artifacts = run_pipeline(fixture_files["clean_strong.pdf"], "r.pdf", sim_config, provider)
    assert run.status == RunStatus.COMPLETED
    assert [n.node for n in run.nodes] == ["intake", "parse", "redact", "score", "aggregate"]
    assert run.aggregate.tier == "strong"
    assert artifacts.redaction_map.redactions
    # The scorer never saw the name or email.
    first = run.parse.record.contact.name.text.split()[0]
    for call in run.node("score").llm_calls:
        assert first not in str(call.request["messages"])
    locs = [e for e in evidence_locations(run) if e.pages]
    assert locs and all(loc.visible_span is not None for loc in locs)


def test_every_node_output_is_recorded_with_model_and_prompt(fixture_files, sim_config, provider):
    run, _ = run_pipeline(fixture_files["clean_review.pdf"], "r.pdf", sim_config, provider)
    for name in ("parse", "score"):
        node = run.node(name)
        assert node.llm_calls and all(c.model == "simulated-v1" and len(c.prompt_hash) == 64 and c.raw_response for c in node.llm_calls)
    assert run.config_hash == sim_config.hash


def test_image_only_goes_to_manual_review(fixture_files, sim_config, provider):
    run, _ = run_pipeline(fixture_files["image_only.pdf"], "r.pdf", sim_config, provider)
    assert run.status == RunStatus.MANUAL_REVIEW and run.aggregate is None
    assert [n.node for n in run.nodes] == ["intake"]


def test_hidden_text_never_reaches_scoring(fixture_files, sim_config, provider):
    clean, _ = run_pipeline(fixture_files["clean_weak.pdf"], "r.pdf", sim_config, provider)
    hidden, _ = run_pipeline(fixture_files["hidden_white.pdf"], "r.pdf", sim_config, provider)
    assert any(f.type == FlagType.HIDDEN_TEXT for f in hidden.flags)
    assert "Kubernetes Kafka distributed" not in hidden.redact.redacted_text
    assert hidden.aggregate.weighted_score == clean.aggregate.weighted_score
    assert hidden.aggregate.needs_attention


def test_injection_moves_naive_model_but_not_baseline(fixture_files, sim_config, provider):
    clean, _ = run_pipeline(fixture_files["clean_weak.pdf"], "r.pdf", sim_config, provider)
    injected, _ = run_pipeline(fixture_files["injection_visible.pdf"], "r.pdf", sim_config, provider)
    assert injected.aggregate.weighted_score <= clean.aggregate.weighted_score
    naive = derive_config(sim_config, {"score": {"model": "simulated-naive"}})
    fooled, _ = run_pipeline(fixture_files["injection_visible.pdf"], "r.pdf", naive, provider)
    assert fooled.aggregate.weighted_score > clean.aggregate.weighted_score


def test_recompute_matches_and_detects_tampering(fixture_files, sim_config, provider):
    run, _ = run_pipeline(fixture_files["clean_strong.pdf"], "r.pdf", sim_config, provider)
    assert recompute(run, sim_config).matches
    tampered = run.model_copy(deep=True)
    tampered.scores[0].final_level = 0
    report = recompute(tampered, sim_config)
    assert not report.matches and "backend_development" in report.differences[0]
    other = derive_config(sim_config, {"score": {"samples": 5}})
    assert not recompute(run, other).matches


@requires_soffice
def test_docx_runs_through_libreoffice_conversion(fixture_files, sim_config, provider):
    run, artifacts = run_pipeline(fixture_files["clean_strong.docx"], "r.docx", sim_config, provider)
    assert run.intake.converted_from_docx
    assert artifacts.render_pdf[:5] == b"%PDF-"
    assert run.aggregate.tier == "strong"


def test_docx_without_libreoffice_goes_to_manual_review(fixture_files, sim_config, provider, monkeypatch):
    import screener.pipeline.intake as intake

    monkeypatch.setattr(intake.shutil, "which", lambda name: None)
    run, artifacts = run_pipeline(fixture_files["hidden_runs.docx"], "r.docx", sim_config, provider)
    assert run.status == RunStatus.MANUAL_REVIEW and artifacts.render_pdf is None
    assert {f.type for f in run.flags} >= {FlagType.UNSUPPORTED_FILE, FlagType.HIDDEN_TEXT}
    assert "Kubernetes Kafka distributed" not in run.intake.visible_text
