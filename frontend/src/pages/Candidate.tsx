import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { api, type ApplicationView, type Me, type Trace } from "../api";
import { PdfViewer, type Highlight } from "../components/PdfViewer";
import { FlagChips, Json, Panel, TierBadge, fmt } from "../components/ui";

function DecisionForm({ app, me, suggested }: { app: ApplicationView; me: Me; suggested: string | null }) {
  const qc = useQueryClient();
  const [decision, setDecision] = useState("");
  const [reason, setReason] = useState("");
  const [override, setOverride] = useState(false);
  const submit = useMutation({
    mutationFn: () => api.post(`/applications/${app.application_id}/decision`, { decision, reason, override }),
    onSuccess: () => {
      setReason("");
      setDecision("");
      qc.invalidateQueries({ queryKey: ["application", app.application_id] });
    },
  });
  if (me.role !== "reviewer") return <p className="muted">Only an assigned reviewer can record a decision.</p>;
  const ready = app.opened_by_me && decision && reason.trim();
  return (
    <form className="decision" onSubmit={(e) => { e.preventDefault(); submit.mutate(); }}>
      {!app.opened_by_me && <p className="warn-text">The document hasn't finished loading for you yet. A decision needs it opened first.</p>}
      <fieldset>
        <legend>Decision</legend>
        {["advance", "hold", "decline"].map((d) => (
          <label key={d}><input type="radio" name="decision" value={d} checked={decision === d} onChange={() => setDecision(d)} /> {d}</label>
        ))}
      </fieldset>
      <label className="block">
        Reason (required)
        <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3} placeholder="What in the document supports this decision?" />
      </label>
      <label><input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} /> I'm overriding the suggested tier{suggested ? ` (${suggested})` : ""}</label>
      <button type="submit" disabled={!ready || submit.isPending}>Record decision</button>
      {submit.isError && <p className="error">{String(submit.error)}</p>}
    </form>
  );
}

export function CandidatePage({ me }: { me: Me }) {
  const { id = "" } = useParams();
  const qc = useQueryClient();
  const [active, setActive] = useState<string | null>(null);
  const [openNode, setOpenNode] = useState<string | null>(null);
  const app = useQuery({ queryKey: ["application", id], queryFn: () => api.get<ApplicationView>(`/applications/${id}`) });
  const docId = app.data?.document_id;
  const pdf = useQuery({
    queryKey: ["document", docId],
    queryFn: async () => {
      const bytes = await api.bytes(`/documents/${docId}/render`);
      qc.invalidateQueries({ queryKey: ["application", id] }); // fetching records the `opened` event
      return bytes;
    },
    enabled: !!docId && !!app.data?.has_render,
    staleTime: Infinity,
  });
  const runId = app.data?.run_id;
  const trace = useQuery({ queryKey: ["trace", runId], queryFn: () => api.get<Trace>(`/runs/${runId}/trace`), enabled: !!runId });
  const reveal = useMutation({
    mutationFn: () => api.post(`/applications/${id}/events`, { event: "scores_revealed" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["trace", runId] }),
  });

  const highlights = useMemo<Highlight[]>(() => {
    const t = trace.data;
    if (!t) return [];
    const hidden: Highlight[] = t.hidden_spans.filter((h) => h.bbox && h.page !== null).map((h) => ({ page: h.page!, bbox: h.bbox!, kind: "hidden", label: `hidden: ${h.reasons.join(", ")}` }));
    const ev: Highlight[] = t.evidence.flatMap((e) => e.pages.map((p) => ({ page: p.page, bbox: p.bbox, kind: "evidence" as const, active: e.criterion === active, label: e.criterion })));
    return [...hidden, ...ev.filter((h) => !active || h.active)];
  }, [trace.data, active]);

  if (app.isError) return <p className="error">{String(app.error)}</p>;
  if (!app.data) return <p className="muted">Loading…</p>;
  const t = trace.data;
  return (
    <div className="candidate">
      <div className="doc-col">
        <Panel title="Document">
          <p className="muted small">Yellow: evidence for the selected criterion. Red: hidden text (never used in scoring).</p>
          {pdf.data ? <PdfViewer data={pdf.data} highlights={highlights} /> : app.data.has_render ? <p className="muted">Loading document…</p> : <p className="muted">No renderable document.</p>}
        </Panel>
      </div>
      <div className="side-col">
        <Panel title="Summary">
          {app.data.ai_opt_out && <p className="chip warn">Candidate opted out of AI screening. Review manually.</p>}
          {t && (
            <>
              <p>
                Suggested tier: <TierBadge tier={t.tier} hidden={t.blind || t.ai_hidden} /> {t.weighted_score !== null && <span className="muted">score {fmt(t.weighted_score)}</span>}
              </p>
              {t.status !== "completed" && <p className="warn-text">Not scored: {t.stop_reason}. Review the document manually.</p>}
              {t.aggregate?.capped_by.length ? <p className="muted small">Must-have criteria at level 0: {t.aggregate.capped_by.join(", ")}</p> : null}
              {t.blind && (
                <button onClick={() => reveal.mutate()} disabled={!app.data.opened_by_me}>
                  Reveal scores (after reading the document)
                </button>
              )}
              <ul className="flags">
                {t.flags.map((f, i) => (
                  <li key={i}><FlagChips flags={[f.type]} /> {f.detail}{f.criterion ? ` (${f.criterion})` : ""}</li>
                ))}
              </ul>
            </>
          )}
          {!runId && !app.data.ai_opt_out && <p className="muted">No pipeline run.</p>}
        </Panel>

        {t && !t.blind && !t.ai_hidden && t.scores.length > 0 && (
          <Panel title="Criteria">
            <table>
              <thead><tr><th>Criterion</th><th>Level</th><th>Samples</th><th /></tr></thead>
              <tbody>
                {t.scores.map((s) => (
                  <tr key={s.criterion} className={active === s.criterion ? "selected" : ""} onClick={() => setActive(active === s.criterion ? null : s.criterion)}>
                    <td>{s.criterion.replaceAll("_", " ")}</td>
                    <td>{s.final_level ?? "—"}</td>
                    <td>{s.samples.map((x) => (x.valid ? x.level : "✗")).join(" · ")}</td>
                    <td>{s.unstable && <span className="chip warn">unstable</span>}{!s.verified && <span className="chip warn">unverified</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {active && (() => {
              const s = t.scores.find((x) => x.criterion === active)!;
              return (
                <div className="evidence">
                  <p className="small">{s.rationale}</p>
                  {s.evidence.map((e, i) => <blockquote key={i}>{e.quote}</blockquote>)}
                  <details>
                    <summary>All samples</summary>
                    {s.samples.map((x) => (
                      <div key={x.index} className="sample">
                        <strong>Sample {x.index + 1}:</strong> claimed {x.claimed_level ?? "—"}, counted {x.valid ? x.level : "invalid"}
                        {x.capped && " (capped: skills-list-only evidence)"}
                        {x.invalid_reason && ` (${x.invalid_reason})`}
                        <ul>{x.evidence.map((e, i) => <li key={i} className={e.verified ? "" : "error"}>{e.quote} {e.verified ? "" : "(not found in document)"}</li>)}</ul>
                      </div>
                    ))}
                  </details>
                </div>
              );
            })()}
          </Panel>
        )}

        <Panel title="Decision">
          {app.data.decisions.map((d, i) => (
            <p key={i} className="small">
              <strong>{d.decision}</strong>{d.override && " (override)"} · {new Date(d.at).toLocaleString()} · {d.reason}
            </p>
          ))}
          {app.data.status !== "decided" && <DecisionForm app={app.data} me={me} suggested={t && !t.blind ? t.tier : null} />}
        </Panel>

        {t && (
          <Panel title="Trace">
            <p className="muted small">Config <code>{t.config_hash.slice(0, 12)}</code></p>
            {t.nodes.map((n) => (
              <div key={n.node} className="node">
                <button className="link" onClick={() => setOpenNode(openNode === n.node ? null : n.node)}>
                  {openNode === n.node ? "▾" : "▸"} {n.node}
                </button>{" "}
                <span className="muted small">
                  {n.models.join(", ")} {n.llm_calls ? `· ${n.llm_calls} calls · ${n.input_tokens}/${n.output_tokens} tokens` : ""} · {n.latency_ms} ms
                </span>
                {openNode === n.node && (
                  <>
                    <p className="small muted">input {n.input_hash.slice(0, 12)} · prompts {n.prompt_hashes.map((h) => h.slice(0, 8)).join(", ") || "none"}</p>
                    <Json value={{ config: n.config, output: n.output }} />
                  </>
                )}
              </div>
            ))}
          </Panel>
        )}
      </div>
    </div>
  );
}
