import { authHeaders } from "./auth";

const API = process.env.TRACELY_API ?? "http://localhost:8000";

/** A non-2xx API response. Thrown (not swallowed) so a backend outage surfaces in the route's
 *  `error.tsx` boundary instead of rendering as an empty state — the worst failure mode for an
 *  observability tool is "looks like there's no data" when the backend is actually down. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public path: string,
  ) {
    super(`Tracely API ${status} on ${path}`);
    this.name = "ApiError";
  }
}

async function apiGet(path: string): Promise<Response> {
  return fetch(`${API}${path}`, { headers: await authHeaders(), cache: "no-store" });
}

/** A signed-out response is not an application error — send the user to sign in again.
 *
 *  The middleware only checks that a session cookie EXISTS, so an EXPIRED one walks straight past
 *  it and fails here instead; sessions last 7 days, so every user hits this eventually. Left to the
 *  error boundary it renders "Something went wrong" over a stack trace, which reads as "the product
 *  is broken" rather than "your session ended".
 *
 *  403 lands here too: the active-workspace cookie can outlive your membership of that workspace
 *  (removed from it, or it was deleted), and every request then 403s with no way out from the UI.
 *  `/login` clears both cookies, which is exactly the reset that case needs.
 */
async function redirectIfSignedOut(res: Response): Promise<void> {
  if (res.status !== 401 && res.status !== 403) return;
  const { redirect } = await import("next/navigation");
  redirect("/login?expired=1");
}

/** GET + parse JSON; throws `ApiError` on any non-2xx (→ error boundary). */
async function getJson<T>(path: string): Promise<T> {
  const res = await apiGet(path);
  await redirectIfSignedOut(res);
  if (!res.ok) throw new ApiError(res.status, path);
  return res.json() as Promise<T>;
}

/** GET a single resource by id: `404` → `null` (→ `notFound()`), other non-2xx throw. */
async function getJsonOrNull<T>(path: string): Promise<T | null> {
  const res = await apiGet(path);
  if (res.status === 404) return null;
  await redirectIfSignedOut(res);
  if (!res.ok) throw new ApiError(res.status, path);
  return res.json() as Promise<T>;
}

export type TraceRow = {
  trace_id: string;
  ts: string;
  spans: number;
  root_name: string;
  agent_id: string;
  has_error: number;
  eval?: string | null;
};

export type EvalScore = {
  name: string;
  evaluation_level: string;
  observation_id: string | null;
  value: number | null;
  string_value?: string;
  verdict: string;
  comment: string;
  data_type: string;
  // present on streamed results (SSE run frames) for cell routing
  trace_id?: string | null;
  session_id?: string | null;
};

export type SpanOut = {
  span_id: string;
  parent_span_id: string;
  name: string;
  type: string;
  level: string;
  status_message: string;
  start_time: string;
  end_time: string | null;
  latency_ms: number | null;
  // Time to the first CONTENT token on a streamed call. For a reasoning model this is the
  // thinking/answering boundary; null when the call wasn't streamed.
  ttft_ms?: number | null;
  agent_id: string;
  agent_run_id: string;
  turn_id: string;
  step_name: string;
  model_id: string;
  tokens: number;
  cost: number;
  metadata: Record<string, string>;
  input: string | null;
  output: string | null;
};

export async function getTraces(): Promise<TraceRow[]> {
  return getJson<TraceRow[]>(`/api/traces?limit=50`);
}

export type Thread = {
  thread: string;
  turns: number;
  first_input: string | null;
  last_output: string | null;
  tokens: number;
  input_tokens?: number;
  output_tokens?: number;
  model?: string;
  cost: number;
  first_ts: string;
  last_ts: string;
  last_trace_id: string;
  failing: number;
  // The conversation's agent — the registry slug its traces are filed under (the tenant, when the
  // SDK declared one). What gates, clusters, cases and scenarios are scoped by; the C-row Agent column.
  agent?: string;
  agent_id?: string;
  metadata?: Record<string, string>;
  scores?: EvalScore[]; // CONVERSATION-level metric results for the C-row columns
  // "" for a real agent run; "eval" | "sim" when the row is one of Tracely's own runs, listed
  // only while the Evals toggle is on (see TracesExplorer).
  internal_kind?: string;
  subject_id?: string; // what that run was about — the trace or conversation it graded/drove
};

// Mirrors `async_reader.SESSION_SORTS`. Sorting is server-side because the list is paginated:
// ordering only the loaded window would make "slowest first" mean "slowest of the 50 on screen".
export type SessionSort = "recent" | "started" | "duration" | "tokens";
export type SortOrder = "asc" | "desc";

export type SessionsQuery = {
  limit?: number;
  offset?: number;
  from?: string | null; // ISO-8601 (UTC) lower bound on a trace's start_time
  to?: string | null; // ISO-8601 (UTC) upper bound (exclusive)
  sort?: SessionSort;
  order?: SortOrder;
  agent?: string; // registry agent id — only that agent's conversations
};

export async function getSessions(opts: SessionsQuery = {}): Promise<Thread[]> {
  const { limit = 50, offset = 0, from, to, sort, order, agent } = opts;
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (from) qs.set("from_ts", from);
  if (to) qs.set("to_ts", to);
  if (sort) qs.set("sort", sort);
  if (order) qs.set("order", order);
  if (agent) qs.set("agent", agent);
  return getJson<Thread[]>(`/api/sessions?${qs.toString()}`);
}

export type ThreadTurn = {
  trace_id: string;
  input: string | null;
  output: string | null;
  tokens: number;
  input_tokens?: number;
  output_tokens?: number;
  model?: string;
  cost: number;
  latency_ms: number;
  ts: string;
  failing: number;
  scores: EvalScore[];
  verdict: string | null;
};

export async function getSession(
  id: string,
): Promise<{ thread_id: string; turns: ThreadTurn[]; scores?: EvalScore[] }> {
  return getJson(`/api/sessions/${id}`);
}

// One sequential column's chain state on a thread (see /sessions/{id}/chain-progress).
export type ChainMetric = {
  score_name: string;
  level: string;
  chained: number;
  turns: number;
  up_to_date: boolean;
  last_payload: Record<string, unknown> | null;
  updated_at: string | null;
};

export async function getChainProgress(
  id: string,
): Promise<{ thread_id: string; metrics: ChainMetric[] }> {
  return getJson(`/api/sessions/${id}/chain-progress`);
}

// ── Hierarchical trace table (conversation → message → step) ──────────────────
// A turn with its spans eagerly attached (detail mode pre-seeds the whole tree).
export type FullTurn = ThreadTurn & { spans: SpanOut[] };
// A conversation node. `turnsData` is present in detail mode and lazily filled in list mode.
export type ConvNode = Thread & { turnsData?: FullTurn[] };

export type TraceDetailData = {
  trace_id: string;
  thread_id?: string | null; // the conversation this trace belongs to (== trace_id when single-turn)
  spans: SpanOut[];
  scores: EvalScore[];
  eval_verdict: string | null;
};

export async function getTrace(traceId: string): Promise<TraceDetailData> {
  return getJson<TraceDetailData>(`/api/traces/${traceId}`);
}

export type Replay = {
  verdict: string;
  candidate_trace_id: string;
  detail: Record<string, unknown>;
  created_at: string | null;
};

export type EvalCase = {
  id: string;
  agent_id: string;
  /** Agent slug — the case is gated as part of THIS agent's suite, nothing else. */
  agent: string | null;
  level: string;
  title: string;
  status: string;
  origin: string;
  source_trace_id: string;
  input_digest: string;
  match_mode: string;
  fail_to_pass_validated: boolean;
  assertions: Record<string, unknown>;
  reference_trajectory: { steps: { kind: string; name: string; level: string }[] };
  created_at: string | null;
  last_verdict?: string | null;
  replays?: Replay[];
};

/** A page of a growing table, plus how many rows exist in total. Lists that only ever grow
 *  (cases, gates, clusters) return this so a page can show "12–24 of 318" without loading 318. */
export type Page<T> = { items: T[]; total: number };

export const PAGE_SIZE = 25;

export async function getCases(
  limit = PAGE_SIZE,
  offset = 0,
): Promise<Page<EvalCase> & { by_agent: Record<string, number> }> {
  return getJson<Page<EvalCase> & { by_agent: Record<string, number> }>(
    `/api/cases?limit=${limit}&offset=${offset}`,
  );
}

export async function getCase(caseId: string): Promise<EvalCase | null> {
  return getJsonOrNull<EvalCase>(`/api/cases/${caseId}`);
}

/** One user turn, plus what the agent is optionally expected to do with it. Both expectation
 *  fields default to empty: a turn with neither is graded only by the project's own evaluators,
 *  exactly as production traffic is. `tools` is checked deterministically and needs the agent's
 *  own spans on the trace; `expect` is LLM-judged and only needs the reply. */
export type ScenarioTurn = {
  message: string;
  expect: string;
  tools: string[];
};

/** A multi-turn conversation Tracely drives against the agent's own endpoint. `SCRIPTED` replays
 *  `turns` (authored, or imported from a real thread); `ADVERSARIAL` improvises toward `goal`. */
export type Scenario = {
  id: string;
  agent_id: string;
  title: string;
  kind: "SCRIPTED" | "ADVERSARIAL";
  turns: ScenarioTurn[];
  goal: string;
  max_turns: number;
  // ADVERSARIAL only: OpenRouter model id the attacker uses. "" = the server's default.
  attacker_model: string;
  source_thread_id: string;
  enabled: boolean;
  created_at: string | null;
};

/** Where Tracely calls the agent. The token is write-only — the API returns `has_token`, never
 *  the value, so it can't leak into a server-rendered page. */
export type AgentEndpoint = {
  agent_id: string;
  configured: boolean;
  url?: string;
  auth_header?: string;
  auth_scheme?: string;
  has_token?: boolean;
  reply_path?: string;
  session_key?: string;
  // Dotted path to a session id the endpoint MINTS in its response. Empty = we supply the id
  // under `session_key`; set = turn 1 sends none and later turns echo what came back.
  session_path?: string;
  timeout_s?: number;
  extra_headers?: Record<string, string>;
  // Merged into every request body — tenant_id, locale, channel. Query params need no
  // equivalent: they ride along in `url`, which is posted verbatim.
  extra_body?: Record<string, unknown>;
};

export async function getScenarios(agent?: string): Promise<Scenario[]> {
  return getJson<Scenario[]>(`/api/scenarios${agent ? `?agent=${encodeURIComponent(agent)}` : ""}`);
}

export async function getAgentEndpoint(agentRef: string): Promise<AgentEndpoint | null> {
  return getJsonOrNull<AgentEndpoint>(`/api/agents/${encodeURIComponent(agentRef)}/endpoint`);
}

/** A workspace alert. One row = one condition + the channels it notifies. `condition.type` is
 *  either a windowed threshold (evaluated every 5 min by the worker) or an event the pipeline
 *  fires the moment it happens — see `domain/monitoring/conditions.py`. */
export type Monitor = {
  id: string;
  name: string;
  description: string;
  /** Agent slug (or id) this alert is scoped to. "" = the whole workspace. */
  target_agent: string;
  condition: {
    type: string;
    score_name?: string;
    threshold?: number;
    window_minutes?: number;
    min_samples?: number;
    /** Event conditions only: case-insensitive substring of the failure text. */
    contains?: string;
    env?: string;
  };
  channels: { type: string; url?: string; to?: string; headers?: Record<string, string> }[];
  /** The action, as a flow: ordered steps whose graph lives in `flow_layout`. Empty = the legacy
   *  channel list above still does the notifying. */
  steps?: {
    id: string;
    order_index: number;
    name: string;
    step_type: string;
    config: Record<string, unknown>;
  }[];
  /** React Flow's own `{nodes, edges}`. The API synthesises a chain when a rule has none. */
  flow_layout?: { nodes: unknown[]; edges: unknown[] } | null;
  enabled: boolean;
  min_interval_seconds: number;
  last_evaluated_at: string | null;
  last_fired_at: string | null;
  last_fired_summary: string;
  created_at: string | null;
};

export async function getMonitors(): Promise<Monitor[]> {
  return getJson<Monitor[]>(`/api/monitors`);
}

export async function getMonitor(monitorId: string): Promise<Monitor | null> {
  return getJsonOrNull<Monitor>(`/api/monitors/${encodeURIComponent(monitorId)}`);
}

export type Stats = {
  traces: number;
  spans: number;
  failing_traces: number;
  auto_failures: number;
  open_clusters: number;
  agents: number;
  cases: number;
};

export async function getStats(): Promise<Stats> {
  return getJson<Stats>(`/api/stats`);
}

/** Evaluator rows for this project (the traces table's columns). Only the fields the
 *  activation checklist reads — the editor fetches the full shape client-side. */
/** The workspace's evaluation columns. Narrow on purpose — the callers need the count, the
 *  enabled flag and (on /settings/alerts) the score name an alert can watch. */
export async function getEvaluators(): Promise<
  { id: string; enabled: boolean; name?: string; score_name?: string; level?: string }[]
> {
  return getJson<{ id: string; enabled: boolean; name?: string; score_name?: string; level?: string }[]>(
    `/api/evaluators`,
  );
}

// Per-evaluator LLM-judge cost over the last `days` — server-component shape (the browser
// equivalent lives in `lib/evaluators.ts:getEvaluatorCost` for the Columns-menu chip).
export type EvaluatorCostRow = {
  runs: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd_cents: number;
  model: string;
};
export type EvaluatorCostPayload = {
  evaluators: Record<string, EvaluatorCostRow>;
  summary: {
    days: number;
    traces_in_window: number;
    total_runs: number;
    total_input_tokens: number;
    total_output_tokens: number;
    total_cost_usd_cents: number;
  };
};

export async function getEvaluatorCost(days = 30): Promise<EvaluatorCostPayload> {
  return getJson<EvaluatorCostPayload>(`/api/evaluators/cost?days=${days}`);
}

export type ClusterMember = {
  trace_id: string;
  is_medoid: boolean;
  summary?: string;
  input?: string;
  latency_ms?: number;
  ts?: string;
  /** earliest non-empty span status_message — for crash clusters this IS the failure */
  error?: string;
  /** the FAIL scores that put this trace in the cluster, with the judge's reason */
  failed?: { name: string; reason?: string }[];
};

// A creatable evaluator draft (built-in structural check or LLM-judge rubric) the cluster view
// opens straight in the Add Column editor — see backend evaluator_suggestion.suggest_evaluator.
export type SuggestedEvaluator = {
  name: string;
  description: string;
  kind: "structural" | "llm_judge";
  level: string;
  config: Record<string, unknown>;
  rationale: string;
};

export type FailureCluster = {
  id: string;
  agent: string | null;
  label: string;
  taxonomy: string;
  description?: string;
  proposed_fix?: string;
  severity?: string;
  method?: string;
  count: number;
  status: string;
  candidate_case_id: string | null;
  signature: string;
  first_seen_at: string | null;
  last_seen_at: string | null;
  members?: ClusterMember[];
  // How many traces the cluster actually has — `members` is capped by the detail endpoint so a
  // cluster that fired thousands of times doesn't ship every one of them to the browser.
  member_total?: number;
  histogram?: { t: string; count: number }[];
  suggested_evaluator?: SuggestedEvaluator;
};

/** `minSize` hides clusters with fewer members (>= 2); omitted = the backend default. */
export async function getClusters(
  minSize?: number,
  limit = PAGE_SIZE,
  offset = 0,
): Promise<Page<FailureCluster> & { open: number }> {
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (minSize) qs.set("min_size", String(minSize));
  return getJson<Page<FailureCluster> & { open: number }>(`/api/clusters?${qs.toString()}`);
}

export async function getCluster(id: string): Promise<FailureCluster | null> {
  return getJsonOrNull<FailureCluster>(`/api/clusters/${id}`);
}

/** One graded unit in a gate run. Exactly one of `evaluation_case_id` / `scenario_id` is set —
 *  a replayed regression case, or an emulated conversation. For a scenario, `candidate_trace_id`
 *  holds the conversation (thread) id and `detail.trace_ids` the per-turn traces. */
export type GateCaseResult = {
  title: string;
  verdict: string;
  candidate_trace_id: string;
  evaluation_case_id: string | null;
  scenario_id: string | null;
  detail: Record<string, unknown>;
};

export type GateRun = {
  id: string;
  agent: string | null;
  env: string;
  git_ref: string;
  pr_number: number | null;
  status: string;
  total: number;
  passed: number;
  failed: number;
  skipped: number;
  latency_ms: number;
  total_tokens: number;
  warnings: string[];
  created_at: string | null;
  // A simulated gate is async: CI (and the UI) polls until this is set.
  finished_at?: string | null;
  cases?: GateCaseResult[];
};

export type AgentRow = { id: string; slug: string; display_name: string };

/** The project's registered agents (auto-registered on ingest). Backed by the meta-analysis
 *  selector's endpoint — same list, no reason for a second one. */
export async function getAgents(): Promise<AgentRow[]> {
  return getJson<AgentRow[]>(`/api/meta-analyses/agents`);
}

export async function getGates(limit = PAGE_SIZE, offset = 0): Promise<Page<GateRun>> {
  return getJson<Page<GateRun>>(`/api/gates?limit=${limit}&offset=${offset}`);
}

export async function getGate(gateId: string): Promise<GateRun | null> {
  return getJsonOrNull<GateRun>(`/api/gates/${gateId}`);
}

export type Trends = {
  days: number;
  daily: { date: string; traces: number; failures: number }[];
  gates_daily: { date: string; passed: number; failed: number }[];
  summary: {
    total_traces: number;
    total_failures: number;
    failure_rate: number;
    gate_runs: number;
    gate_pass_rate: number;
    cases: number;
    open_clusters: number;
    resolved_clusters: number;
    mttr_hours: number | null;
  };
};

export async function getTrends(days = 14): Promise<Trends> {
  return getJson<Trends>(`/api/trends?days=${days}`);
}

/** Latency / throughput / cost roll-up. Durations are milliseconds; `cost` comes from OTLP
 *  `cost_details` and is 0 for SDKs that don't report price (the panel derives it from tokens). */
export type Ops = {
  days: number;
  summary: {
    traces: number;
    p50: number;
    p95: number;
    p99: number;
    errors: number;
    error_rate: number;
    traces_per_day: number;
    tokens: number;
    cost: number;
    cost_per_1k_traces: number;
    ttft_p50: number;
    llm_calls: number;
  };
  daily: { date: string; traces: number; p50: number; p95: number; p99: number; errors: number; tokens: number; cost: number }[];
  models: {
    model: string; calls: number; p50: number; p95: number; ttft_p50: number;
    errors: number; tokens: number; input_tokens: number; output_tokens: number; cost: number;
  }[];
  slowest: { name: string; type: string; calls: number; p50: number; p95: number; max: number; errors: number }[];
};

export async function getOps(days = 14): Promise<Ops> {
  return getJson<Ops>(`/api/ops?days=${days}`);
}

export type SharedSession = { thread_id: string; turns: FullTurn[]; scores: EvalScore[] };

/** Read a publicly shared conversation. Deliberately NOT authenticated — the token in the path is
 *  the whole credential, and the visitor has no session to send. `null` on 404 (which the backend
 *  also returns for expired/forged tokens, so a bad link and a private one look identical). */
export async function getSharedSession(token: string): Promise<SharedSession | null> {
  const res = await fetch(`${API}/api/share/${encodeURIComponent(token)}`, { cache: "no-store" });
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiError(res.status, "/api/share");
  return res.json() as Promise<SharedSession>;
}

/** Whether this workspace has its own OpenRouter key. Every LLM-backed feature (judge
 *  evaluators, clustering, meta-analysis, rolling summary, scenario gates) runs on it — there is
 *  no server-wide fallback — so the shell shows a banner when it's missing. Never throws: a key
 *  lookup failing must not take the whole app down, and "assume configured" is the quiet option. */
export async function getLlmKeyConfigured(): Promise<boolean> {
  try {
    const res = await apiGet("/api/project/llm-key");
    if (!res.ok) return true;
    return Boolean(((await res.json()) as { configured?: boolean })?.configured);
  } catch {
    return true;
  }
}

/** Hosted-cloud billing snapshot. `trace_limit: null` = uncapped (unlimited plan, or the
 *  limits don't apply). `billing_enabled: false` = self-hosted — the billing page shows its
 *  "no limits" state and the quota banner never renders. */
export type BillingUsage = {
  billing_enabled: boolean;
  plan: string;
  period: string; // "YYYY-MM" (UTC)
  traces_used: number;
  trace_limit: number | null;
  // "account" = free quota pooled across every workspace this account owns; "workspace" = this
  // project alone (paid plans, or self-host projects with no owning user).
  quota_scope: "account" | "workspace";
};

/** Never throws (same rationale as getLlmKeyConfigured): the shell's quota banner and the
 *  billing page must degrade to silence, not take the app down. Null = unknown. */
export async function getBillingUsage(): Promise<BillingUsage | null> {
  try {
    const res = await apiGet("/api/billing/usage");
    if (!res.ok) return null;
    return (await res.json()) as BillingUsage;
  } catch {
    return null;
  }
}
