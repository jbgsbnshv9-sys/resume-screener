import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { Panel } from "../components/ui";

interface AuditRow { id: number; actor: string; action: string; entity_type: string; entity_id: string; at: string; detail: Record<string, unknown> }

export function AuditPage() {
  const [entity, setEntity] = useState("");
  const [appId, setAppId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const log = useQuery({ queryKey: ["audit", entity], queryFn: () => api.get<AuditRow[]>(`/audit${entity ? `?entity_id=${encodeURIComponent(entity)}` : ""}`) });
  const download = async () => {
    setError(null);
    try {
      const bundle = await api.get<unknown>(`/export/applications/${appId}`);
      const url = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = `audit-${appId}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(String(e));
    }
  };
  return (
    <>
      <Panel title="Export an application">
        <div className="row">
          <input value={appId} onChange={(e) => setAppId(e.target.value)} placeholder="Application ID" aria-label="Application ID" />
          <button onClick={download} disabled={!appId.trim()}>Download JSON bundle</button>
          {error && <span className="error">{error}</span>}
        </div>
      </Panel>
      <Panel title="Audit log (append-only)">
        <input value={entity} onChange={(e) => setEntity(e.target.value)} placeholder="Filter by entity ID" aria-label="Entity ID" />
        <table>
          <thead><tr><th>#</th><th>When</th><th>Actor</th><th>Action</th><th>Entity</th><th>Detail</th></tr></thead>
          <tbody>
            {log.data?.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td><td>{new Date(r.at).toLocaleString()}</td><td>{r.actor}</td><td>{r.action}</td>
                <td><code>{r.entity_type}:{r.entity_id.slice(0, 12)}</code></td><td className="small mono">{JSON.stringify(r.detail)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </>
  );
}
