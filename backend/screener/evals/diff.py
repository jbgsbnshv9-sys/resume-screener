"""Version diff: two configs on the same eval set (the most important view)."""

from __future__ import annotations

from pydantic import BaseModel

from screener.evals.runner import EvalReport


class TierChange(BaseModel):
    case_id: str
    kind: str
    before: str | None
    after: str | None
    score_before: float | None
    score_after: float | None


class MetricChange(BaseModel):
    metric: str
    before: float | None
    after: float | None
    before_ci: tuple[float | None, float | None]
    after_ci: tuple[float | None, float | None]


class VersionDiff(BaseModel):
    before_config: str
    after_config: str
    eval_set_id: str
    same_eval_set: bool
    tier_changes: list[TierChange]
    metric_changes: list[MetricChange]
    counterfactual_changes: list[MetricChange]
    impact_ratio_changes: dict[str, dict[str, tuple[float | None, float | None]]]
    gate_before: bool | None
    gate_after: bool | None


def diff_reports(a: EvalReport, b: EvalReport) -> VersionDiff:
    ra = {r.case_id: r for r in a.results if r.repeat == 0}
    rb = {r.case_id: r for r in b.results if r.repeat == 0}
    changes = [
        TierChange(case_id=cid, kind=ra[cid].kind, before=ra[cid].tier, after=rb[cid].tier, score_before=ra[cid].weighted_score, score_after=rb[cid].weighted_score)
        for cid in sorted(set(ra) & set(rb))
        if ra[cid].tier != rb[cid].tier
    ]
    metrics = [
        MetricChange(metric=m, before=a.metrics[m].value if m in a.metrics else None, after=b.metrics[m].value if m in b.metrics else None,
                     before_ci=(a.metrics[m].lower, a.metrics[m].upper) if m in a.metrics else (None, None),
                     after_ci=(b.metrics[m].lower, b.metrics[m].upper) if m in b.metrics else (None, None))
        for m in sorted(set(a.metrics) | set(b.metrics))
    ]
    cf = []
    ratios: dict[str, dict[str, tuple[float | None, float | None]]] = {}
    for section_a, section_b, prefix in ((a.counterfactual, b.counterfactual, ""), (a.sensitivity_redaction_off, b.sensitivity_redaction_off, "redaction_off.")):
        for attr in sorted(set(section_a) | set(section_b)):
            ea, eb = section_a.get(attr), section_b.get(attr)
            for metric in ("mean_abs_delta", "tier_flip_rate"):
                x = getattr(ea, metric) if ea else None
                y = getattr(eb, metric) if eb else None
                cf.append(MetricChange(metric=f"{prefix}{attr}.{metric}", before=x.value if x else None, after=y.value if y else None,
                                       before_ci=(x.lower, x.upper) if x else (None, None), after_ci=(y.lower, y.upper) if y else (None, None)))
            groups = set(ea.impact_ratios if ea else {}) | set(eb.impact_ratios if eb else {})
            ratios[f"{prefix}{attr}"] = {g: ((ea.impact_ratios.get(g) if ea else None), (eb.impact_ratios.get(g) if eb else None)) for g in sorted(groups)}
    return VersionDiff(
        before_config=a.config_hash,
        after_config=b.config_hash,
        eval_set_id=b.eval_set_id,
        same_eval_set=a.eval_set_hash == b.eval_set_hash,
        tier_changes=changes,
        metric_changes=metrics,
        counterfactual_changes=cf,
        impact_ratio_changes=ratios,
        gate_before=a.gate.passed if a.gate else None,
        gate_after=b.gate.passed if b.gate else None,
    )


def format_diff(d: VersionDiff) -> str:
    def f(x: float | None) -> str:
        return "  n/a" if x is None else f"{x:5.3f}"

    def ci(c: tuple[float | None, float | None]) -> str:
        return "" if c[0] is None else f" [{c[0]:.3f},{c[1]:.3f}]"

    lines = [f"Version diff {d.before_config[:12]} -> {d.after_config[:12]} on {d.eval_set_id}" + ("" if d.same_eval_set else "  (WARNING: eval sets differ)")]
    lines.append(f"Gate: {d.gate_before} -> {d.gate_after}")
    lines.append(f"\nCandidates whose tier changed: {len(d.tier_changes)}")
    for c in d.tier_changes[:50]:
        lines.append(f"  {c.case_id:<40} {c.kind:<14} {c.before} -> {c.after}   ({f(c.score_before)} -> {f(c.score_after)})")
    lines.append("\nMetrics (value [95% CI]):")
    for m in d.metric_changes:
        lines.append(f"  {m.metric:<28} {f(m.before)}{ci(m.before_ci):<16} -> {f(m.after)}{ci(m.after_ci)}")
    lines.append("\nCounterfactuals:")
    for m in d.counterfactual_changes:
        lines.append(f"  {m.metric:<44} {f(m.before)}{ci(m.before_ci):<16} -> {f(m.after)}{ci(m.after_ci)}")
    lines.append("\nImpact ratios (selection rate of 'strong' relative to the highest group):")
    for attr, groups in d.impact_ratio_changes.items():
        for g, (x, y) in groups.items():
            mark = "  <0.8" if y is not None and y < 0.8 else ""
            lines.append(f"  {attr + '.' + g:<44} {f(x)} -> {f(y)}{mark}")
    return "\n".join(lines)
