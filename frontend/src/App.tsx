import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, Navigate, Route, Routes } from "react-router-dom";
import { api, getToken, setToken, type Me } from "./api";
import { CandidatePage } from "./pages/Candidate";
import { ConfigPage, ConfigsPage } from "./pages/Configs";
import { DiffPage } from "./pages/Diff";
import { AuditPage } from "./pages/Audit";
import { QueuePage, RequisitionsPage } from "./pages/Queue";

function Login({ onLogin }: { onLogin: (token: string) => void }) {
  const [token, setValue] = useState("");
  return (
    <div className="login">
      <h1>Resume Screener</h1>
      <p className="muted">Paste your API token. Tokens are issued by an admin (<code>screener user create</code>).</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onLogin(token.trim());
        }}
      >
        <input type="password" value={token} onChange={(e) => setValue(e.target.value)} placeholder="API token" aria-label="API token" />
        <button type="submit" disabled={!token.trim()}>Sign in</button>
      </form>
    </div>
  );
}

export function App() {
  const qc = useQueryClient();
  const [token, setTokenState] = useState<string | null>(getToken());
  const login = (t: string | null) => {
    setToken(t);
    qc.clear();
    setTokenState(t);
  };
  const me = useQuery({ queryKey: ["me", token], queryFn: () => api.get<Me>("/me"), enabled: !!token });
  if (!token || me.isError) return <Login onLogin={login} />;
  if (!me.data) return <div className="muted pad">Loading…</div>;
  const role = me.data.role;
  const canReview = role === "reviewer" || role === "admin" || role === "auditor";
  const canConfig = role === "rubric_owner" || role === "admin" || role === "auditor";
  return (
    <div className="app">
      <nav className="top">
        <strong>Resume Screener</strong>
        {canReview && <Link to="/requisitions">Review queue</Link>}
        {canConfig && <Link to="/configs">Configs &amp; evals</Link>}
        {canConfig && <Link to="/diff">Version diff</Link>}
        {(role === "auditor" || role === "admin") && <Link to="/audit">Audit</Link>}
        <span className="spacer" />
        <span className="muted">{me.data.email} · {role.replace("_", " ")}</span>
        <button
          className="link"
          onClick={() => login(null)}
        >
          Sign out
        </button>
      </nav>
      <p className="notice">Suggested tiers order the queue. They are never decisions: every candidate is decided by a person who has read the document.</p>
      <main>
        <Routes>
          <Route path="/" element={<Navigate to={canReview ? "/requisitions" : "/configs"} replace />} />
          <Route path="/requisitions" element={<RequisitionsPage me={me.data} />} />
          <Route path="/requisitions/:id" element={<QueuePage me={me.data} />} />
          <Route path="/applications/:id" element={<CandidatePage me={me.data} />} />
          <Route path="/configs" element={<ConfigsPage me={me.data} />} />
          <Route path="/configs/:hash" element={<ConfigPage me={me.data} />} />
          <Route path="/diff" element={<DiffPage />} />
          <Route path="/audit" element={<AuditPage />} />
        </Routes>
      </main>
    </div>
  );
}
