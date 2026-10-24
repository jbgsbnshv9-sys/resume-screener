import pytest

from screener.config import derive_config
from screener.evals.diff import diff_reports, format_diff
from screener.evals.evalset import build_eval_set, load_eval_set, save_eval_set
from screener.evals.gate import GateConfig, evaluate_gate, load_gate
from screener.evals.metrics import cohen_kappa, wilson
from tests.conftest import CONFIGS

GATE = load_gate(CONFIGS / "evals" / "default-gate.yaml")


@pytest.fixture(scope="module")
def small_set():
    return build_eval_set("t", "backend-engineer", n_base=18, n_counterfactual_parents=3, n_injection_parents=4, seed=3)


@pytest.fixture(scope="module")
def baseline_report(small_set, sim_config, provider):
    from screener.evals.runner import run_eval

    report = run_eval(small_set, sim_config, provider, repeats=2)
    report.gate = evaluate_gate(report, GATE.model_copy(update={"min_base_cases": 10}))
    return report


def test_kappa_and_wilson_known_values():
    assert cohen_kappa([("a", "a"), ("b", "b")], labels=("a", "b")) == 1.0
    pairs = [("a", "a"), ("a", "b"), ("b", "a"), ("b", "b")]
    assert cohen_kappa(pairs, labels=("a", "b")) == 0.0
    w = wilson(5, 10)
    assert w.value == 0.5 and 0.2 < w.lower < 0.25 and 0.75 < w.upper < 0.8


def test_eval_set_round_trips(tmp_path, small_set):
    path = tmp_path / "set.jsonl"
    save_eval_set(small_set, path)
    loaded = load_eval_set(path)
    assert loaded.content_hash == small_set.content_hash
    kinds = {c.kind for c in loaded.cases}
    assert kinds == {"base", "counterfactual", "injection", "hidden"}
    cf = [c for c in loaded.cases if c.kind == "counterfactual"]
    assert {c.attribute for c in cf} == {"name", "employment_gap", "credential_country", "non_native_english", "polished_rewrite", "affinity_org"}


def test_baseline_report_has_every_metric_with_intervals(baseline_report):
    for metric in ("tier_accuracy", "tier_kappa", "consistency_flip_rate", "evidence_verification_rate", "injection_robustness", "hidden_text_detection", "hidden_text_robustness"):
        est = baseline_report.metrics[metric]
        assert est.n > 0 and est.lower is not None and est.upper is not None
    assert baseline_report.metrics["hidden_text_detection"].value == 1.0
    assert set(baseline_report.sensitivity_redaction_off) == {"name", "affinity_org"}
    assert any("labeler" in f for f in baseline_report.gate.flags)


def test_small_eval_sets_fail_on_interval_width(baseline_report):
    # 18 base cases: point estimates are fine but the conservative bounds are too wide to pass.
    failed = {c.metric for c in baseline_report.gate.checks if not c.passed}
    assert baseline_report.metrics["unscored_base_rate"].value < 0.15 and "unscored_base_rate" in failed


def test_full_eval_set_baseline_passes_gate(sim_config, provider):
    from screener.evals.runner import run_eval

    report = run_eval(build_eval_set("full", "backend-engineer"), sim_config, provider, repeats=2)
    report.gate = evaluate_gate(report, GATE)
    assert report.gate.passed, [c for c in report.gate.checks if not c.passed]


def test_gate_uses_conservative_bound(baseline_report):
    strict = GateConfig(min_base_cases=10, thresholds={"tier_accuracy": {"min": 0.99}})
    check = next(c for c in evaluate_gate(baseline_report, strict).checks if c.metric == "tier_accuracy")
    assert check.bound == "lower" and check.observed == baseline_report.metrics["tier_accuracy"].lower


def test_gate_requires_minimum_cases(baseline_report):
    assert not evaluate_gate(baseline_report, GATE).passed  # 18 base cases < 50


def test_naive_model_fails_injection_gate(small_set, sim_config, provider):
    from screener.evals.runner import run_eval

    naive = derive_config(sim_config, {"score": {"model": "simulated-naive"}})
    report = run_eval(small_set, naive, provider, repeats=1, sensitivity=False)
    report.gate = evaluate_gate(report, GATE.model_copy(update={"min_base_cases": 10}))
    assert report.metrics["injection_robustness"].value < 1.0
    assert "injection_robustness" in [c.metric for c in report.gate.checks if not c.passed]


def test_biased_model_fails_on_redaction_off_sensitivity(small_set, sim_config, provider):
    from screener.evals.runner import run_eval

    biased = derive_config(sim_config, {"score": {"model": "simulated-biased"}})
    report = run_eval(small_set, biased, provider, repeats=1)
    report.gate = evaluate_gate(report, GATE.model_copy(update={"min_base_cases": 10}))
    assert report.counterfactual["name"].mean_abs_delta.value == 0.0  # redaction hides it...
    assert report.sensitivity_redaction_off["name"].mean_abs_delta.value > 0.05  # ...but the model is biased
    assert not report.gate.passed


def test_version_diff(baseline_report, small_set, sim_config, provider):
    from screener.evals.runner import run_eval

    naive = derive_config(sim_config, {"score": {"model": "simulated-naive"}})
    after = run_eval(small_set, naive, provider, repeats=1, sensitivity=False)
    d = diff_reports(baseline_report, after)
    assert d.same_eval_set
    assert any(c.kind == "injection" for c in d.tier_changes) or any(m.metric == "injection_robustness" and m.after < m.before for m in d.metric_changes)
    text = format_diff(d)
    assert "injection_robustness" in text and "Impact ratios" in text
