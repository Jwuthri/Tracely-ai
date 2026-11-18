/** Audits: the wire types and the few pure rules the UI applies to them.
 *
 *  An audit names a behaviour, a target and how to judge it; a run drives every seed; each
 *  rollout is one attacker ⇄ target conversation, judged by a matrix of
 *  (dimension × model × sample) and reduced to a verdict. See `backend/tracely/domain/audit`. */

export type DimensionKind = "behaviour" | "validity" | "info";

export type AuditDimension = {
  name: string;
  kind: DimensionKind;
  description: string;
  rubric: string;
  threshold: number;
  models: string[];
  samples: number;
  // Catalog entries only: where the dimension comes from ("petri" for the vendored library).
  source?: string;
  tags?: string[];
};

/** A reference to an image or audio file — never the bytes. `text` is what a voice message says,
 *  or what an uploaded file shows, for readers (and judges) that cannot see or hear it. */
export type MediaRef = {
  asset: string;
  mime: string;
  kind: "image" | "audio";
  name: string;
  size: number;
  text: string;
};

export type TargetTool = { name: string; description: string; parameters: Record<string, unknown> };

export type AuditConfig = {
  target: {
    model: string;
    control: "auditor" | "fixed";
    system_prompt: string;
    tools: TargetTool[];
    temperature: number;
    reasoning_effort: string;
  };
  attacker: {
    models: string[];
    max_turns: number;
    prefill: boolean;
    rollback: boolean;
    realism_filter?: boolean;
    // Explore runs: draft this many moves a turn and play the one a picker prefers.
    best_of?: number;
    // Modal tools the attacker has: "image" = generate images, "voice" = send voice messages.
    media?: ("image" | "voice")[];
  };
  // "sandbox": the target gets a real machine and its shell/file tools really execute.
  environment?: { mode: "synthetic" | "sandbox"; packs: string[]; network?: "none" | "allowlist" };
  dimensions: AuditDimension[];
  judge_models: string[];
  limits: { budget_usd: number; reps: number };
};

export type RunSummary = {
  status?: "OK" | "NO_COVERAGE";
  total?: number;
  counted?: number;
  elicitation_rate?: number | null;
  verdicts?: Record<string, number>;
  dimensions?: Record<
    string,
    { kind: DimensionKind; n: number; mean: number; flagged: number; disputed: number; agreement: number | null }
  >;
  spend_usd?: number;
};

export type AuditRun = {
  id: string;
  audit_id: string;
  status: "RUNNING" | "DONE" | "CANCELLED";
  mode: "measure" | "explore";
  budget_usd: number;
  spend_usd: number;
  total: number;
  done: number;
  summary: RunSummary;
  created_at: string | null;
  finished_at: string | null;
};

export type Audit = {
  id: string;
  name: string;
  agent: string;
  behavior: string;
  config: AuditConfig;
  dimensions: AuditDimension[];
  created_at: string | null;
  seeds: number;
  last_run: AuditRun | null;
};

export type AuditSeed = {
  id: string;
  title: string;
  instruction: string;
  tags: string[];
  origin: string;
  parent_seed_id: string | null;
  attachments: MediaRef[];
  enabled: boolean;
  created_at: string | null;
};

export type AuditDetail = Omit<Audit, "seeds" | "last_run"> & { seeds: AuditSeed[]; runs: AuditRun[] };

export type Highlight = { message_id: string; quote: string };

export type JudgeAnswer = {
  model: string;
  scores: number[];
  mean: number | null;
  justification: string;
  highlights: Highlight[];
  error: string;
};

export type DimensionResult = {
  name: string;
  kind: DimensionKind;
  threshold: number;
  graded: boolean;
  value: number | null;
  flagged: boolean;
  agreement: number | null;
  disputed: boolean;
  judges: JudgeAnswer[];
};

export type RolloutVerdict =
  | "ELICITED" | "CLEAN" | "INVALID" | "INCOMPLETE" | "UNGRADED" | "ERROR" | "SKIPPED" | "";

export type AuditRollout = {
  id: string;
  seed_id: string;
  title: string;
  rep: number;
  position: number;
  // 0 = the first pass; 1 = added by an explore run's deepen pass.
  stage?: number;
  phase: "PENDING" | "DRIVING" | "JUDGING" | "DONE";
  verdict: RolloutVerdict;
  results: DimensionResult[];
  detail: {
    ended?: string;
    turns?: number;
    branches?: number;
    error?: string;
    reason?: string;
    attacker_model?: string;
    trace_ids?: string[];
  };
  spend_usd: number;
  conversation_id: string;
  finished_at: string | null;
};

export type AuditRunDetail = AuditRun & {
  audit: { id: string; name: string };
  config: AuditConfig & { behavior?: string };
  dimensions: AuditDimension[];
  rollouts: AuditRollout[];
};

export type TranscriptNode = {
  id: string;
  parent: string;
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  branch: number;
  reasoning?: string;
  prefill?: string;
  tool_calls?: { id: string; name: string; args: Record<string, unknown> }[];
  tool_call_id?: string;
  name?: string;
  status?: string;
  resolution?: string;
  attachments?: MediaRef[];
};

export type AttackerMessage = {
  role: "user" | "assistant" | "tool";
  content: string | null;
  tool_calls?: { id: string; function: { name: string; arguments: string } }[];
  tool_call_id?: string;
};

export type RolloutDetail = AuditRollout & {
  run_id: string;
  seed: { title?: string; instruction?: string };
  transcript: {
    system_prompt: string;
    control: string;
    tools: { name: string; description: string; environment: string }[];
    nodes: TranscriptNode[];
    branches: { n: number; from: string; kind: string }[];
    head: string;
    attacker: AttackerMessage[];
  };
};

export type AuditPreset = {
  key: string;
  name: string;
  behaviour: string;
  dimensions: string[];
  seeds: { title: string; instruction: string }[];
};

export type AuditCatalog = {
  validity: AuditDimension[];
  behaviours: AuditDimension[];
  presets: AuditPreset[];
  // Whether this deployment can run a target on a real machine, and give it allowlisted web access.
  sandbox?: { available: boolean; network?: boolean };
  library?: { petri: { seeds: number; tags: { tag: string; count: number }[] } };
  // The axes a seed can be varied along.
  variations?: { name: string; description: string }[];
  // Every tool of each pack as the target sees it; a trailing `?` marks an optional argument.
  packs?: Record<string, { name: string; args: string[]; description: string }[]>;
};

/** The tool packs a sandboxed target can be given, in the order the form lists them. */
export const PACKS: { key: string; label: string; hint: string }[] = [
  { key: "dev", label: "Coding", hint: "shell, files" },
  { key: "office", label: "Office", hint: "mail, calendar, chat, documents" },
  { key: "support", label: "Support", hint: "customers, orders, refunds, tickets" },
  { key: "data", label: "Data", hint: "a SQL warehouse" },
  { key: "devops", label: "DevOps", hint: "services, deploys, CI, secrets" },
];

/** A file as the base64 the attachment endpoint takes. The type is decided by the server from
 *  the bytes, so nothing here trusts the file's name or its `type`. */
export async function fileBase64(file: Blob): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

type Tone = "ok" | "fail" | "warn" | "info" | "neutral";

/** How a rollout's verdict reads. ELICITED is the finding — red, like a failing test. INVALID,
 *  INCOMPLETE and UNGRADED are amber on purpose: none of them says the target behaved, and a grey
 *  badge would let a run that judged nothing look merely quiet. */
export function verdictTone(v: string): Tone {
  if (v === "ELICITED" || v === "ERROR") return "fail";
  if (v === "CLEAN") return "ok";
  if (v === "INVALID" || v === "INCOMPLETE" || v === "UNGRADED") return "warn";
  return "neutral";
}

/** One line on what a verdict means, for the badge's tooltip. */
export function verdictMeaning(v: string): string {
  switch (v) {
    case "ELICITED": return "The target showed the behaviour, in a rollout the judges found valid.";
    case "CLEAN": return "A valid, complete rollout in which the target did not show the behaviour.";
    case "INVALID": return "Not counted: the target noticed the test, or the scenario or attacker was broken.";
    case "INCOMPLETE": return "Not counted: cut short (budget, time or an error) before the attacker finished.";
    case "UNGRADED": return "Not counted: the judges did not produce a result.";
    case "ERROR": return "The rollout failed before the target could be tested.";
    case "SKIPPED": return "Never ran.";
    default: return "";
  }
}

/** `0.3333` → `33%`; null (nothing graded) → an em dash, never `0%` — "the target never does
 *  this" and "we measured nothing" must not look alike. */
export function formatRate(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return "—";
  return `${Math.round(rate * 100)}%`;
}

export function formatUsd(n: number | null | undefined): string {
  const v = Number(n ?? 0);
  return v > 0 && v < 0.01 ? "<$0.01" : `$${v.toFixed(2)}`;
}

/** The tone of one dimension's score cell. A validity dimension over its threshold is amber, not
 *  red: it does not say the target misbehaved, it says the rollout proves nothing. */
export function resultTone(r: DimensionResult): Tone {
  if (!r.graded) return "warn";
  if (r.kind === "info") return "neutral";
  if (!r.flagged) return "ok";
  return r.kind === "validity" ? "warn" : "fail";
}

/** Ids on the conversation the target actually ended with: head back to the root. Everything
 *  else in `nodes` is a branch the attacker rolled back. */
export function finalPath(nodes: TranscriptNode[], head: string): Set<string> {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const out = new Set<string>();
  for (let cur = head; cur && byId.has(cur); cur = byId.get(cur)!.parent) out.add(cur);
  return out;
}

/** Group a transcript's nodes by branch, in the order branches were created. */
export function byBranch(
  nodes: TranscriptNode[],
  branches: { n: number; from: string; kind: string }[],
): { n: number; from: string; kind: string; nodes: TranscriptNode[] }[] {
  return branches
    .map((b) => ({ ...b, nodes: nodes.filter((n) => n.branch === b.n) }))
    .filter((b) => b.nodes.length > 0);
}

/** `2 of 6 · judging` while a run is going, for the header and the list. */
export function runProgress(run: Pick<AuditRun, "status" | "done" | "total">): string {
  if (run.status === "RUNNING") return `${run.done} of ${run.total} rollouts`;
  return `${run.total} rollout${run.total === 1 ? "" : "s"}`;
}
