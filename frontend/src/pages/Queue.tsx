import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type Alert, type Me, type QueueItem, type Requisition } from "../api";
import { FlagChips, Panel, TierBadge } from "../components/ui";

export function RequisitionsPage({ me }: { me: Me }) {
  const reqs = useQuery({ queryKey: ["requisitions"], queryFn: () => api.get<Requisition[]>("/requisitions") });
  const alerts = useQuery({ queryKey: ["alerts"], queryFn: () => api.get<Alert[]>("/alerts") });
  return (
    <>
      {!!alerts.data?.length && (
        <Panel title={`Alerts (${alerts.data.length})`}>
          <ul className="alerts">
            {alerts.data.map((a) => (
              <li key={a.type + a.application_id}>
                {a.type === "not_opened" ? "Not opened within the review SLA" : "On hold past the hold limit"} ·{" "}
                <Link to={`/applications/${a.application_id}`}>open candidate</Link> <span className="muted">since {new Date(a.since).toLocaleDateString()}</span>
              </li>
            ))}
          </ul>
        </Panel>
      )}
      <Panel title="Requisitions">
        {reqs.data?.length === 0 && <p className="muted">{me.role === "reviewer" ? "You aren't assigned to any requisitions yet." : "No requisitions yet."}</p>}
        <table>
          <thead>
            <tr><th>Opening</th><th>Role</th><th>Status</th><th>Pinned config</th></tr>
          </thead>
          <tbody>
            {reqs.data?.map((r) => (
              <tr key={r.id}>
                <td><Link to={`/requisitions/${r.id}`}>{r.opening_ref}</Link></td>
                <td>{r.role_id}</td>
                <td>{r.status}</td>
                <td><code>{r.pinned_config_hash?.slice(0, 12) ?? "not pinned yet"}</code></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </>
  );
}

function UploadForm({ requisitionId }: { requisitionId: string }) {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [optOut, setOptOut] = useState(false);
  const [jurisdiction, setJurisdiction] = useState("unknown");
  const upload = useMutation({
    mutationFn: () => {
      const form = new FormData();
      form.append("file", file!);
      form.append("ai_opt_out", String(optOut));
      form.append("jurisdiction", jurisdiction);
      const path = file!.name.endsWith(".zip") ? "import" : "applications";
      return api.upload(`/requisitions/${requisitionId}/${path}`, form);
    },
    onSuccess: () => {
      setFile(null);
      qc.invalidateQueries({ queryKey: ["queue", requisitionId] });
    },
  });
  return (
    <form className="row" onSubmit={(e) => { e.preventDefault(); upload.mutate(); }}>
      <input type="file" accept=".pdf,.docx,.zip" onChange={(e) => setFile(e.target.files?.[0] ?? null)} aria-label="Resume file" />
      <label><input type="checkbox" checked={optOut} onChange={(e) => setOptOut(e.target.checked)} /> Candidate opted out of AI screening</label>
      <input value={jurisdiction} onChange={(e) => setJurisdiction(e.target.value)} placeholder="Jurisdiction" aria-label="Jurisdiction" style={{ width: 120 }} />
      <button type="submit" disabled={!file || upload.isPending}>{upload.isPending ? "Uploading…" : "Upload"}</button>
      {upload.isError && <span className="error">{String(upload.error)}</span>}
    </form>
  );
}

export function QueuePage({ me }: { me: Me }) {
  const { id = "" } = useParams();
  const [filter, setFilter] = useState<"open" | "flagged" | "all">("open");
  const queue = useQuery({ queryKey: ["queue", id], queryFn: () => api.get<QueueItem[]>(`/requisitions/${id}/queue`) });
  const items = useMemo(() => {
    const all = queue.data ?? [];
    if (filter === "open") return all.filter((i) => i.status !== "decided");
    if (filter === "flagged") return all.filter((i) => i.needs_attention);
    return all;
  }, [queue.data, filter]);
  return (
    <Panel
      title="Review queue"
      actions={
        <div className="row">
          {(["open", "flagged", "all"] as const).map((f) => (
            <button key={f} className={filter === f ? "tab active" : "tab"} onClick={() => setFilter(f)}>{f}</button>
          ))}
        </div>
      }
    >
      {(me.role === "admin" || me.role === "reviewer") && <UploadForm requisitionId={id} />}
      {queue.isError && <p className="error">{String(queue.error)}</p>}
      <p className="muted small">Candidates without a visible tier (unscored, opted out, or blind review) are listed first so no one is stranded at the bottom.</p>
      <table>
        <thead>
          <tr><th>Candidate</th><th>Suggested tier</th><th>Flags</th><th>Status</th><th>Opened</th><th>Open by</th></tr>
        </thead>
        <tbody>
          {items.map((i) => (
            <tr key={i.application_id} className={i.overdue ? "overdue" : ""}>
              <td><Link to={`/applications/${i.application_id}`}>{i.application_id.slice(0, 8)}</Link>{i.ai_opt_out && <span className="chip">manual review only</span>}</td>
              <td><TierBadge tier={i.tier} hidden={i.tier_hidden} /></td>
              <td>{i.needs_attention && <span className="chip warn">needs attention</span>}<FlagChips flags={i.flags} /></td>
              <td>{i.status.replace("_", " ")}</td>
              <td>{i.opened ? "yes" : "no"}</td>
              <td className={i.overdue ? "error" : ""}>{new Date(i.open_by).toLocaleDateString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {queue.isPending && <p className="muted">Loading…</p>}
      {queue.isSuccess && !items.length && <p className="muted">Nothing here.</p>}
    </Panel>
  );
}
