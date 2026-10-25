import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type ConfigSummary, type ConfigView, type EvalRun, type Me } from "../api";
import { Panel, fmt } from "../components/ui";

export function ConfigsPage({ me }: { me: Me }) {
  const qc = useQueryClient();
  const configs = useQuery({ queryKey: ["configs"], queryFn: () => api.get<ConfigSummary[]>("/configs") });
  const [yaml, setYaml] = useState("");
  const create = useMutation({
    mutationFn: () => api.post<ConfigView>("/configs", { yaml }),
    onSuccess: () => {
      setYaml("");
      qc.invalidateQueries({ queryKey: ["configs"] });
    },
  });
  return (
    <>
      <Panel title="Pipeline configs">
        <table>
          <thead><tr><th>Hash</th><th>Role</th><th>Rubric</th><th>Status</th><th>Created</th></tr></thead>
          <tbody>
            {configs.data?.map((c) => (
              <tr key={c.hash}>
                <td><Link to={`/configs/${c.hash}`}><code>{c.hash.slice(0, 12)}</code></Link></td>
                <td>{c.role_id}</td>
                <td>{c.rubric_ref}</td>
                <td><span className={`badge status-${c.status}`}>{c.status}</span></td>
                <td>{new Date(c.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
      {(me.role === "rubric_owner" || me.role === "admin") && (
        <Panel title="Register a draft config">
          <p className="muted small">Paste pipeline YAML. The rubric it references must exist in <code>configs/rubrics/</code>. Configs are immutable: a change is a new hash.</p>
          <form onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
            <textarea className="mono" rows={10} value={yaml} onChange={(e) => setYaml(e.target.value)} aria-label="Config YAML" />
            <button type="submit" disabled={!yaml.trim() || create.isPending}>Register</button>
            {create.isError && <p className="error">{String(create.error)}</p>}
          </form>
        </Panel>
      )}
    </>
  );
}

function Metric({ name, est, check }: { name: string; est: { value: number | null; lower: number | null; upper: number | null; n: number }; check?: { passed: boolean; bound: string; threshold: number } }) {
  return (
    <tr className={check && !check.passed ? "fail" : ""}>
      <td>{name.replaceAll("_", " ")}</td>
      <td>{fmt(est.value)}</td>
      <td className="muted">[{fmt(est.lower)}, {fmt(est.upper)}]</td>
      <td>{est.n}</td>
      <td>{check ? `${check.passed ? "pass" : "FAIL"} (${check.bound} vs ${check.threshold})` : ""}</td>
    </tr>
  );
}

export function EvalRunView({ id }: { id: string }) {
  const run = useQuery({ queryKey: ["eval-run", id], queryFn: () => api.get<EvalRun>(`/eval-runs/${id}`) });
  if (!run.data) return <p className="muted">Loading eval…</p>;
  const r = run.data;
  const checkFor = (m: string) => r.gate.checks.find((c) => c.metric === m);
  return (
    <div>
      <p>Gate: <strong className={r.passed ? "ok" : "error"}>{r.passed ? "passed" : "failed"}</strong></p>
      {r.gate.flags.map((f) => <p key={f} className="warn-text small">⚑ {f}</p>)}
      <table>
        <thead><tr><th>Metric</th><th>Value</th><th>95% CI</th><th>n</th><th>Gate</th></tr></thead>
        <tbody>{Object.entries(r.metrics).map(([k, v]) => <Metric key={k} name={k} est={v} check={checkFor(k)} />)}</tbody>
      </table>
      <h3>Counterfactuals</h3>
      <table>
        <thead><tr><th>Attribute</th><th>Pairs</th><th>Mean |Δ score|</th><th>Tier flip rate</th><th>Impact ratios</th></tr></thead>
        <tbody>
          {Object.entries(r.counterfactual).map(([k, v]) => (
            <tr key={k}>
              <td>{k.replaceAll("_", " ")}</td>
              <td>{v.pairs}</td>
              <td>{fmt(v.mean_abs_delta.value)} <span className="muted">[{fmt(v.mean_abs_delta.lower)}, {fmt(v.mean_abs_delta.upper)}]</span></td>
              <td>{fmt(v.tier_flip_rate.value)} <span className="muted">[{fmt(v.tier_flip_rate.lower)}, {fmt(v.tier_flip_rate.upper)}]</span></td>
              <td className="small">{Object.entries(v.impact_ratios).map(([g, x]) => <span key={g} className={x < 0.8 ? "chip warn" : "chip"}>{g}: {x.toFixed(2)}</span>)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>With redaction off (model sensitivity)</h3>
      <table>
        <thead><tr><th>Attribute</th><th>Mean |Δ score|</th><th>Tier flip rate</th><th>Impact ratios</th></tr></thead>
        <tbody>
          {Object.entries(r.sensitivity_redaction_off).map(([k, v]) => (
            <tr key={k}>
              <td>{k}</td>
              <td>{fmt(v.mean_abs_delta.value)} <span className="muted">[{fmt(v.mean_abs_delta.lower)}, {fmt(v.mean_abs_delta.upper)}]</span></td>
              <td>{fmt(v.tier_flip_rate.value)}</td>
              <td className="small">{Object.entries(v.impact_ratios).map(([g, x]) => <span key={g} className={x < 0.8 ? "chip warn" : "chip"}>{g}: {x.toFixed(2)}</span>)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ConfigPage({ me }: { me: Me }) {
  const { hash = "" } = useParams();
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["config", hash], queryFn: () => api.get<ConfigView>(`/configs/${hash}`), refetchInterval: (q) => (q.state.data?.eval_running ? 3000 : false) });
  const sets = useQuery({ queryKey: ["eval-sets"], queryFn: () => api.get<string[]>("/eval-sets") });
  const [evalSet, setEvalSet] = useState("");
  const [justification, setJustification] = useState("");
  const [shown, setShown] = useState<string | null>(null);
  const startEval = useMutation({
    mutationFn: () => api.post(`/configs/${hash}/evals`, { eval_set: evalSet || sets.data?.[0], repeats: 2 }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["config", hash] }),
  });
  const promote = useMutation({
    mutationFn: () => api.post(`/configs/${hash}/promote`, justification.trim() ? { override_justification: justification } : {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["config", hash] }),
  });
  if (!cfg.data) return <p className="muted">Loading…</p>;
  const c = cfg.data;
  const latest = shown ?? c.eval_runs[0]?.id ?? null;
  return (
    <>
      <Panel title={`Config ${c.hash.slice(0, 12)}`}>
        <p>
          <span className={`badge status-${c.status}`}>{c.status}</span> · role {c.role_id} · rubric {c.rubric_ref} · created by {c.created_by}
          {c.promoted_by && ` · promoted by ${c.promoted_by}`}
        </p>
        {c.override_justification && <p className="warn-text">Promoted by admin override: {c.override_justification}</p>}
        {!c.hash_still_resolves && <p className="error">The rubric, a prompt, or a term list changed since this config was hashed. It can't be used. Register a new version.</p>}
        <details><summary>YAML</summary><pre className="json">{c.yaml}</pre></details>
        <h3>Criteria</h3>
        <ul>{c.rubric.criteria.map((k) => <li key={k.id}>{k.name} · weight {k.weight}{k.must_have && " · must-have"}</li>)}</ul>
        <h3>Rubric lint</h3>
        {c.lint.length === 0 ? <p className="muted">No proxy-risk findings.</p> : (
          <ul>{c.lint.map((f, i) => <li key={i} className={f.acknowledged ? "" : "warn-text"}>{f.criterion}.{f.field}: <strong>{f.rule}</strong> ("{f.match}"): {f.explanation} {f.acknowledged ? "(acknowledged)" : "(needs acknowledgment before promotion)"}</li>)}</ul>
        )}
      </Panel>
      <Panel title="Evaluation">
        {(me.role === "rubric_owner" || me.role === "admin") && (
          <form className="row" onSubmit={(e) => { e.preventDefault(); startEval.mutate(); }}>
            <select value={evalSet} onChange={(e) => setEvalSet(e.target.value)} aria-label="Eval set">
              {sets.data?.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            <button type="submit" disabled={c.eval_running || startEval.isPending}>{c.eval_running ? "Eval running…" : "Run eval suite"}</button>
            {startEval.isError && <span className="error">{String(startEval.error)}</span>}
          </form>
        )}
        <div className="row">
          {c.eval_runs.map((e) => (
            <button key={e.id} className={latest === e.id ? "tab active" : "tab"} onClick={() => setShown(e.id)}>
              {new Date(e.created_at).toLocaleString()} · {e.passed ? "passed" : "failed"}
            </button>
          ))}
        </div>
        {latest ? <EvalRunView id={latest} /> : <p className="muted">No eval runs yet.</p>}
      </Panel>
      {me.role === "admin" && c.status !== "active" && c.status !== "retired" && (
        <Panel title="Promote to active">
          <p className="muted small">Needs a passing eval on this exact hash and acknowledged lint findings. An override needs a written justification and is recorded in the audit log.</p>
          <form onSubmit={(e) => { e.preventDefault(); promote.mutate(); }}>
            <textarea rows={2} value={justification} onChange={(e) => setJustification(e.target.value)} placeholder="Override justification (only if promoting without a passing eval)" aria-label="Override justification" />
            <button type="submit" disabled={promote.isPending}>Promote</button>
            {promote.isError && <p className="error">{String(promote.error)}</p>}
            {promote.isSuccess && <p className="ok">Promoted.</p>}
          </form>
        </Panel>
      )}
    </>
  );
}
