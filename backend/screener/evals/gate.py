"""Promotion gate. Every threshold is applied to the conservative end of the
interval: the lower bound for metrics that must be high, the upper bound for
metrics that must be low. A config passes only if every check passes (or an
admin records an override with a justification, outside this module)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from screener.evals.metrics import Estimate
from screener.evals.runner import Check, EvalReport, GateResult


class Threshold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min: float | None = None
    max: float | None = None


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_base_cases: int = 50
    thresholds: dict[str, Threshold]
    # Applied to every counterfactual attribute.
    counterfactual: dict[Literal["mean_abs_delta", "tier_flip_rate"], Threshold] = Field(default_factory=dict)
    # Applied to name/affinity counterfactuals re-run with redaction off. Redaction can
    # leak (prose, emails), so a model that is biased underneath shouldn't pass on redaction alone.
    sensitivity: dict[Literal["mean_abs_delta", "tier_flip_rate"], Threshold] = Field(default_factory=dict)
    # The four-fifths rule is a flag, never a pass/fail line.
    impact_ratio_flag_below: float = 0.8


def load_gate(path: Path | str) -> GateConfig:
    return GateConfig.model_validate(yaml.safe_load(Path(path).read_text()))


def _check(metric: str, est: Estimate, t: Threshold) -> list[Check]:
    checks: list[Check] = []
    if t.min is not None:
        observed = est.lower
        checks.append(Check(metric=metric, bound="lower", observed=observed, threshold=t.min, passed=observed is not None and observed >= t.min, note="" if observed is not None else "not measured"))
    if t.max is not None:
        observed = est.upper
        checks.append(Check(metric=metric, bound="upper", observed=observed, threshold=t.max, passed=observed is not None and observed <= t.max, note="" if observed is not None else "not measured"))
    return checks


def evaluate_gate(report: EvalReport, gate: GateConfig) -> GateResult:
    checks: list[Check] = []
    n_base = sum(1 for r in report.results if r.kind == "base" and r.repeat == 0)
    checks.append(Check(metric="base_cases", bound="count", observed=float(n_base), threshold=float(gate.min_base_cases), passed=n_base >= gate.min_base_cases))
    for metric, t in gate.thresholds.items():
        est = report.metrics.get(metric, Estimate.empty())
        checks.extend(_check(metric, est, t))
    for attribute, rep in report.counterfactual.items():
        for metric, t in gate.counterfactual.items():
            checks.extend(_check(f"counterfactual.{attribute}.{metric}", getattr(rep, metric), t))
    for attribute, rep in report.sensitivity_redaction_off.items():
        for metric, t in gate.sensitivity.items():
            checks.extend(_check(f"sensitivity_redaction_off.{attribute}.{metric}", getattr(rep, metric), t))
    flags = []
    for section, reps in (("counterfactual", report.counterfactual), ("sensitivity_redaction_off", report.sensitivity_redaction_off)):
        for attribute, rep in reps.items():
            for g in rep.flagged_groups:
                flags.append(f"{section}.{attribute}: impact ratio for {g} is {rep.impact_ratios[g]:.2f} (< {gate.impact_ratio_flag_below})")
    if len(report.labelers) < 2:
        flags.append(f"labels come from {len(report.labelers)} labeler(s) {report.labelers}; add independent human labels before real use")
    return GateResult(passed=all(c.passed for c in checks), checks=checks, flags=flags)
