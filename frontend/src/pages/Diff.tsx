import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type ConfigSummary, type ConfigView } from "../api";
import { Panel, fmt } from "../components/ui";

interface DiffResponse {
  text: string;
  diff: {
    before_config: string; after_config: string; same_eval_set: boolean; gate_before: boolean | null; gate_after: boolean | null;
    tier_changes: { case_id: string; kind: string; before: string | null; after: string | null; score_before: number | null; score_after: number | null }[];
    metric_changes: { metric: string; before: number | null; after: number | null; before_ci: [number | null, number | null]; after_ci: [number | null, number | null] }[];
    counterfactual_changes: { metric: string; before: number | null; after: number | null }[];
    impact_ratio_changes: Record<string, Record<string, [number | null, number | null]>>;
  };
}

function useEvalRuns() {
  const configs = useQuery({ queryKey: ["configs"], queryFn: () => api.get<ConfigSummary[]>("/configs") });
  const views = useQuery({
    queryKey: ["config-views", configs.data?.map((c) => c.hash).join(",")],
    queryFn: () => Promise.all((configs.data ?? []).map((c) => api.get<ConfigView>(`/configs/${c.hash}`))),
    enabled: !!configs.data,
  });
  return (views.data ?? []).flatMap((v) => v.eval_runs.map((e) => ({ ...e, config: v.hash, status: v.status })));
}

function ImpactRatios({ changes }: { changes: DiffResponse["diff"]["impact_ratio_changes"] }) {
  const rows = Object.entries(changes).flatMap(([attr, groups]) => Object.entries(groups).map(([g, [x, y]]) => ({ key: `${attr}.${g}`, x, y })));
  const notable = rows.filter((r) => r.x !== r.y || (r.x !== null && r.x < 0.8) || (r.y !== null && r.y < 0.8));
  const table = (rs: typeof rows) => (
    <table>
      <thead><tr><th>Group</th><th>Before</th><th>After</th></tr></thead>
      <tbody>{rs.map((r) => <tr key={r.key} className={r.y !== null && r.y < 0.8 ? "fail" : ""}><td>{r.key}</td><td>{fmt(r.x, 2)}</td><td>{fmt(r.y, 2)}</td></tr>)}</tbody>
    </table>
  );
  return (
    <>
      <h3>Impact ratios that changed or fall below 0.8</h3>
      {notable.length ? table(notable) : <p className="muted">None. Every group's ratio is unchanged and at or above 0.8.</p>}
      <details><summary>All {rows.length} groups</summary>{table(rows)}</details>
    </>
  );
}

export function DiffPage() {
  const runs = useEvalRuns();
  const [before, setBefore] = useState("");
  const [after, setAfter] = useState("");
  const diff = useQuery({ queryKey: ["diff", before, after], queryFn: () => api.get<DiffResponse>(`/eval-diff?before=${before}&after=${after}`), enabled: !!before && !!after });
  const option = (r: (typeof runs)[number]) => <option key={r.id} value={r.id}>{r.config.slice(0, 12)} ({r.status}) · {r.eval_set_id} · {new Date(r.created_at).toLocaleString()} · {r.passed ? "passed" : "failed"}</option>;
  const d = diff.data?.diff;
  return (
    <>
      <Panel title="Version diff">
        <p className="muted small">Compare two eval runs on the same eval set: which candidates changed tier, and how agreement, consistency and fairness metrics moved.</p>
        <div className="row">
          <select value={before} onChange={(e) => setBefore(e.target.value)} aria-label="Before"><option value="">Before…</option>{runs.map(option)}</select>
          <select value={after} onChange={(e) => setAfter(e.target.value)} aria-label="After"><option value="">After…</option>{runs.map(option)}</select>
        </div>
        {diff.isError && <p className="error">{String(diff.error)}</p>}
      </Panel>
      {d && (
        <>
          {!d.same_eval_set && <p className="error">These eval runs used different eval sets; the comparison isn't like for like.</p>}
          <Panel title={`Gate: ${d.gate_before} → ${d.gate_after}`}>
            <table>
              <thead><tr><th>Metric</th><th>Before</th><th>After</th><th>Change</th></tr></thead>
              <tbody>
                {d.metric_changes.map((m) => (
                  <tr key={m.metric}>
                    <td>{m.metric.replaceAll("_", " ")}</td>
                    <td>{fmt(m.before)} <span className="muted">[{fmt(m.before_ci[0])}, {fmt(m.before_ci[1])}]</span></td>
                    <td>{fmt(m.after)} <span className="muted">[{fmt(m.after_ci[0])}, {fmt(m.after_ci[1])}]</span></td>
                    <td>{m.before !== null && m.after !== null ? (m.after - m.before >= 0 ? "+" : "") + (m.after - m.before).toFixed(3) : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>
          <Panel title={`Candidates whose tier changed (${d.tier_changes.length})`}>
            <table>
              <thead><tr><th>Case</th><th>Kind</th><th>Before</th><th>After</th></tr></thead>
              <tbody>{d.tier_changes.map((c) => <tr key={c.case_id}><td><code>{c.case_id}</code></td><td>{c.kind}</td><td>{c.before} ({fmt(c.score_before, 2)})</td><td>{c.after} ({fmt(c.score_after, 2)})</td></tr>)}</tbody>
            </table>
          </Panel>
          <Panel title="Counterfactuals and impact ratios">
            <table>
              <thead><tr><th>Metric</th><th>Before</th><th>After</th></tr></thead>
              <tbody>{d.counterfactual_changes.map((m) => <tr key={m.metric}><td>{m.metric}</td><td>{fmt(m.before)}</td><td>{fmt(m.after)}</td></tr>)}</tbody>
            </table>
            <ImpactRatios changes={d.impact_ratio_changes} />
          </Panel>
        </>
      )}
    </>
  );
}
