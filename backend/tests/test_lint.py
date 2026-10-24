from screener.config import LintAcknowledgment, load_rubric
from screener.lint.rubric_lint import lint_rubric, unacknowledged


def _with_text(**changes):
    rubric = load_rubric("backend-engineer-2026q4@v1")
    c0 = rubric.criteria[0]
    levels = dict(c0.levels)
    if "level3" in changes:
        levels[3] = levels[3].model_copy(update={"description": changes.pop("level3")})
    c0 = c0.model_copy(update={**changes, "levels": levels})
    return rubric.model_copy(update={"criteria": [c0, *rubric.criteria[1:]]})


def test_example_rubric_is_clean():
    assert lint_rubric(load_rubric("backend-engineer-2026q4@v1")) == []


def test_flags_proxy_language_in_any_field():
    rubric = _with_text(description="5+ years of experience at a top-tier company; must be a culture fit", level3="Digital native who graduated within the last 3 years")
    rules = {(f.field, f.rule) for f in lint_rubric(rubric)}
    assert ("description", "min_tenure") in rules
    assert ("description", "prestige") in rules
    assert ("description", "culture_fit") in rules
    assert ("levels.3.description", "age_correlated") in rules


def test_acknowledgment_clears_finding():
    rubric = _with_text(description="Builds services; no employment gaps")
    assert [f.rule for f in unacknowledged(rubric)] == ["employment_gap"]
    ack = LintAcknowledgment(criterion="backend_development", rule="employment_gap", field="description", acknowledged_by="owner@example.com", reason="Reworded in v2; acknowledged for this draft")
    rubric = rubric.model_copy(update={"lint_acknowledgments": [ack]})
    assert unacknowledged(rubric) == []
    assert lint_rubric(rubric)[0].acknowledged
