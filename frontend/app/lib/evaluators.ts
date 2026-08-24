"use client";

import type { EvalScore } from "./api";

// ── Evaluator (= evaluation column) client API ─────────────────────────────────
// Browser-side helpers over the Next proxy routes (/api/evaluators*, /api/evaluations/run).
// Server components use lib/api.ts; client components (the table, the Add Column modal) use these.

export type EvaluatorLevel = "CONVERSATION" | "AGENT_RUN" | "SPAN" | "TOOL" | "GENERATION" | "CHAIN";

export type EvaluatorOutputType = "score" | "number" | "boolean" | "text" | "json";

export type EvaluatorConfig = {
  prompt?: string;
  threshold?: number;
  output_type?: EvaluatorOutputType;
  output_schema?: Record<string, unknown>; // JSON Schema, for output_type "json"
  execution_mode?: "batch" | "sequential"; // sequential = chain items of this metric
  is_advanced?: boolean; // prompt uses @VARIABLE templates (set server-side from the prompt)
  template_variables?: string[]; // refs used, e.g. ["HISTORY", "CURRENT_STEP.tool_call"] (informational)
  depends_on?: string[]; // score_names of evaluators whose results are injected as context
  model?: string;
  span_types?: string[];
  check?: string; // structural evaluators
  params?: Record<string, unknown>;
};

export type JudgeModelOption = { id: string; label: string };
export type JudgeModels = { default: string; models: JudgeModelOption[] };

export type EvaluatorDef = {
  id: string;
  name: string;
  description: string;
  kind: "structural" | "llm_judge";
  score_name: string;
  level: EvaluatorLevel;
  enabled: boolean;
  target_agent?: string;
  target_env?: string;
  sampling?: number;
  config: EvaluatorConfig;
  created_at?: string | null;
};

export type EvaluatorTemplate = {
  name: string;
  description: string;
  kind: "structural" | "llm_judge";
  score_name: string;
  level: EvaluatorLevel;
  config: EvaluatorConfig;
  recommended?: boolean;
  category?: string;
  installed: boolean;
};

export type EvaluatorDraft = {
  name: string;
  description: string;
  kind: "structural" | "llm_judge";
  level: EvaluatorLevel;
  config: EvaluatorConfig;
  score_name?: string;
};

/** An evaluator write that the API refused, carrying the status so callers can explain WHY
 *  rather than echoing the backend's wording at the user. */
export class EvaluatorActionError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
    this.name = "EvaluatorActionError";
  }
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* keep statusText */
    }
    throw new EvaluatorActionError(detail, res.status);
  }
  return res.json() as Promise<T>;
}

/** What to show when deleting a metric column fails.
 *
 *  The raw API text is right but unhelpful in a banner: a machine credential gets back
 *  "insufficient role", which tells a user neither what happened nor what to do. Changing the
 *  workspace needs a signed-in person — any role, owner through member — because an ingest key
 *  lives in CI logs and `.env` files, and a leak should cost traces, not the columns people built.
 */
export function deleteColumnError(err: unknown, columnName: string): string {
  const status = err instanceof EvaluatorActionError ? err.status : 0;
  if (status === 403) {
    return `Not allowed to delete “${columnName}”. Deleting a column needs a signed-in Tracely account — an ingest key can send and read traces, but can't change the workspace.`;
  }
  if (status === 401) {
    return `Your session has expired — sign in again to delete “${columnName}”.`;
  }
  if (status === 404) {
    return `“${columnName}” no longer exists — someone may have deleted it already. Refresh to update the table.`;
  }
  const detail = err instanceof Error && err.message ? err.message : "unknown error";
  return `Could not delete “${columnName}”: ${detail}`;
}

export async function listEvaluators(): Promise<EvaluatorDef[]> {
  const res = await fetch("/api/evaluators", { cache: "no-store" });
  if (!res.ok) return [];
  return res.json();
}

// Per-evaluator LLM-judge token usage + USD-cents cost (priced live from OpenRouter when
// reachable, else a static fallback table) — what each judge column actually costs to run.
export type EvaluatorCost = {
  runs: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd_cents: number; // 0 when pricing for `model` is unknown
  model: string;
};

export type CostSummary = {
  days: number;
  traces_in_window: number; // production traces (env != 'ci') — denominator for $/1k traces
  total_runs: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost_usd_cents: number;
};

export type EvaluatorCostPayload = {
  evaluators: Record<string, EvaluatorCost>;
  summary: CostSummary;
};

const _EMPTY_COST: EvaluatorCostPayload = {
  evaluators: {},
  summary: {
    days: 30, traces_in_window: 0, total_runs: 0,
    total_input_tokens: 0, total_output_tokens: 0, total_cost_usd_cents: 0,
  },
};

export async function getEvaluatorCost(days = 30): Promise<EvaluatorCostPayload> {
  const res = await fetch(`/api/evaluators/cost?days=${days}`, { cache: "no-store" });
  if (!res.ok) return { ..._EMPTY_COST, summary: { ..._EMPTY_COST.summary, days } };
  return res.json();
}

export async function createEvaluator(draft: EvaluatorDraft & { enabled?: boolean }): Promise<EvaluatorDef> {
  return jsonOrThrow(await fetch("/api/evaluators", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(draft),
  }));
}

export async function updateEvaluator(
  id: string,
  patch: Partial<Pick<EvaluatorDef, "name" | "description" | "level" | "enabled" | "config">>,
): Promise<EvaluatorDef> {
  return jsonOrThrow(await fetch(`/api/evaluators/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(patch),
  }));
}

export async function deleteEvaluator(id: string): Promise<void> {
  await jsonOrThrow(await fetch(`/api/evaluators/${encodeURIComponent(id)}`, { method: "DELETE" }));
}

export async function listTemplates(): Promise<EvaluatorTemplate[]> {
  const res = await fetch("/api/evaluators/templates", { cache: "no-store" });
  if (!res.ok) return [];
  return res.json();
}

export async function listJudgeModels(): Promise<JudgeModels> {
  const res = await fetch("/api/evaluators/models", { cache: "no-store" });
  if (!res.ok) return { default: "", models: [] };
  return res.json();
}

export async function generateEvaluator(description: string): Promise<EvaluatorDraft> {
  return jsonOrThrow(await fetch("/api/evaluators/generate", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ description }),
  }));
}

// ── Advanced-mode live preview ──────────────────────────────────────────────────
// Resolve an @VARIABLE prompt against a real conversation/turn/step (no LLM) for the editor's
// preview pane.

export type ResolvePreviewRequest = {
  prompt: string;
  level: EvaluatorLevel;
  thread_id?: string;
  trace_id?: string;
  span_id?: string;
};

export type ResolvedPreview = {
  resolved_prompt: string;
  variables_used: string[];
  variables_missing: string[];
  level: string;
};

export async function resolvePromptPreview(req: ResolvePreviewRequest): Promise<ResolvedPreview> {
  return jsonOrThrow(await fetch("/api/evaluators/resolve", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(req),
  }));
}

// ── Streaming runs ──────────────────────────────────────────────────────────────

export type RunScope = { evaluator_ids?: string[]; thread_ids?: string[]; trace_ids?: string[] };

export type RunEvent =
  | { type: "start"; targets: number; evaluators: number }
  | { type: "result"; score: EvalScore }
  | { type: "target_done"; target: string; scores: number }
  | { type: "target_error"; target: string; detail: string }
  | { type: "error"; detail: string }
  | { type: "done" };

// POST the run and decode the SSE frames (`data: <json>` lines, `data: [DONE]` terminator),
// invoking `onEvent` per frame. Resolves when the stream ends; rejects on a non-2xx response.
export async function streamEvaluationRun(
  scope: RunScope,
  onEvent: (e: RunEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch("/api/evaluations/run", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "text/event-stream" },
    body: JSON.stringify(scope),
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* keep statusText */
    }
    throw new Error(detail || "evaluation run failed");
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const payload = line.slice("data: ".length).trim();
      if (payload === "[DONE]") return;
      let ev: RunEvent;
      try {
        ev = JSON.parse(payload) as RunEvent;
      } catch {
        continue; /* skip malformed frame */
      }
      onEvent(ev);
    }
  }
}

// ── Display helpers shared by the table + modal ─────────────────────────────────

// Which C/M/S column group an evaluator's level renders under.
export function levelGroup(level: EvaluatorLevel | string): "C" | "M" | "S" {
  if (level === "CONVERSATION") return "C";
  if (level === "AGENT_RUN") return "M";
  return "S";
}

export const LEVEL_LABEL: Record<string, string> = {
  CONVERSATION: "Conversation",
  AGENT_RUN: "Message",
  SPAN: "Step",
  TOOL: "Tool step",
  GENERATION: "Generation step",
  CHAIN: "Chain step",
};
