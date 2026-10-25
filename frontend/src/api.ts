// Thin client for the backend API. The token lives in sessionStorage so it's
// gone when the tab closes; nothing candidate-related is stored client-side.

const TOKEN_KEY = "screener.token";

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: the user re-enters the token */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown, raw = false): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload: BodyInit | undefined;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const res = await fetch(`/api${path}`, { method, headers, body: payload });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(res.status, detail);
  }
  return (raw ? res.arrayBuffer() : res.json()) as Promise<T>;
}

export const api = {
  get: <T,>(path: string) => request<T>("GET", path),
  post: <T,>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  upload: <T,>(path: string, form: FormData) => request<T>("POST", path, form),
  bytes: (path: string) => request<ArrayBuffer>("GET", path, undefined, true),
};

export type Role = "admin" | "rubric_owner" | "reviewer" | "auditor";
export interface Me { id: string; email: string; role: Role; display_name: string }
export interface Requisition { id: string; role_id: string; opening_ref: string; status: string; pinned_config_hash: string | null }
export interface QueueItem {
  application_id: string; status: string; ai_opt_out: boolean; run_id: string | null; run_status: string | null;
  tier: string | null; tier_hidden: boolean; needs_attention: boolean; flags: string[]; opened: boolean;
  applied_at: string; open_by: string; overdue: boolean;
}
export interface Flag { node: string; type: string; detail: string; criterion: string | null }
export interface Evidence { quote: string; verified: boolean; in_entry: boolean }
export interface Sample { index: number; claimed_level: number | null; level: number | null; evidence: Evidence[]; rationale: string; valid: boolean; invalid_reason: string | null; capped: boolean }
export interface Score { criterion: string; samples: Sample[]; final_level: number | null; verified: boolean; unstable: boolean; evidence: Evidence[]; rationale: string }
export interface Bbox { page: number; bbox: [number, number, number, number] }
export interface HiddenSpan { text: string; reasons: string[]; source: string; page: number | null; bbox: [number, number, number, number] | null }
export interface EvidenceLocation { criterion: string; quote: string; pages: Bbox[] }
export interface TraceNode { node: string; input_hash: string; config: unknown; output: unknown; models: string[]; prompt_hashes: string[]; input_tokens: number; output_tokens: number; latency_ms: number; llm_calls: number }
export interface Trace {
  run_id: string; application_id: string; document_id: string | null; config_hash: string; status: string; stop_reason: string | null;
  ai_hidden: boolean; blind: boolean; tier: string | null; weighted_score: number | null;
  aggregate: { tier: string; uncapped_tier: string; capped_by: string[]; weighted_score: number; score_range: [number, number]; contributions: { criterion: string; weight: number; level: number | null; must_have: boolean }[] } | null;
  flags: Flag[]; hidden_spans: HiddenSpan[]; evidence: EvidenceLocation[]; scores: Score[]; nodes: TraceNode[];
}
export interface ApplicationView {
  application_id: string; status: string; ai_opt_out: boolean; run_id: string | null; run_status: string | null; requisition_id: string;
  document_id: string | null; has_render: boolean; blind_first_look: boolean; scores_revealed: boolean; opened_by_me: boolean;
  decisions: { decision: string; reason: string; override: boolean; suggested_tier: string | null; reviewer_id: string; at: string }[];
}
export interface ConfigSummary { hash: string; role_id: string; status: string; rubric_ref: string; created_at: string }
export interface LintFinding { criterion: string; field: string; rule: string; match: string; explanation: string; acknowledged: boolean }
export interface ConfigView extends ConfigSummary {
  yaml: string; created_by: string; promoted_by: string | null; override_justification: string | null; hash_still_resolves: boolean;
  lint: LintFinding[]; eval_runs: { id: string; passed: boolean; eval_set_id: string; created_at: string }[]; eval_running: boolean;
  rubric: { criteria: { id: string; name: string; weight: number; must_have: boolean }[] };
}
export interface Estimate { value: number | null; lower: number | null; upper: number | null; n: number }
export interface GateCheck { metric: string; bound: string; observed: number | null; threshold: number; passed: boolean; note: string }
export interface EvalRun {
  id: string; config_hash: string; passed: boolean; metrics: Record<string, Estimate>;
  gate: { passed: boolean; checks: GateCheck[]; flags: string[] };
  counterfactual: Record<string, { pairs: number; mean_abs_delta: Estimate; tier_flip_rate: Estimate; impact_ratios: Record<string, number>; flagged_groups: string[] }>;
  sensitivity_redaction_off: Record<string, { pairs: number; mean_abs_delta: Estimate; tier_flip_rate: Estimate; impact_ratios: Record<string, number>; flagged_groups: string[] }>;
}
export interface Alert { type: string; application_id: string; requisition_id: string; since: string }
