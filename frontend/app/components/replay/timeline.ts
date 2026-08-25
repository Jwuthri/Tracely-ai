// Pure helpers for the conversation replay stage (tested — no React, no fetch).

export type ReplayActor = {
  /** `customer` is the one synthetic actor: the person the office works for, whose `ask`
   *  events carry each turn's user message. */
  id: string; name: string; kind: "agent" | "subagent" | "customer"; parent: string;
  depth: number; first_ms: number; last_ms: number; events: number; errors: number;
};

export type ReplayEvent = {
  t_ms: number; dur_ms: number; actor: string; kind: string; name: string;
  status: "ok" | "error"; model: string; detail: string; span_id: string;
  trace_id: string; turn_id: string;
  /** True for spans that BRACKET other work (a turn wrapper, a sub-agent envelope) rather
   *  than being work themselves — they must never make an agent look "working". */
  container?: boolean;
  /** Where the actor performs this event: desk | computer | library | peer. */
  station?: string;
  /** Actor key of the callee when this event is a handoff. */
  delegate_to?: string;
  /** The handoff text (a DELEGATE span's input, or the tool-call arguments). */
  say?: string;
  /** Tools this beat REQUESTED — an llm that answered with a call instead of words. Indexed
   *  names when ingest caught them, else dug out of the payload (`_output_calls`). */
  calls?: string[];
  /** Spend on this span (usage/cost maps summed server-side) — the wall ticker. */
  tokens?: number;
  cost?: number;
};

/** An event placed on the PLAY clock (gaps squeezed), keeping its real timestamp. */
export type PlayEvent = ReplayEvent & { pt: number; pdur: number; index: number };

export const KIND_STYLE: Record<string, { label: string; color: string; icon: string }> = {
  turn: { label: "turn", color: "#7aa2ff", icon: "▶" },
  spawn: { label: "sub-agent", color: "#c084fc", icon: "✦" },
  delegate: { label: "delegate", color: "#38bdf8", icon: "⇄" },
  llm: { label: "llm", color: "#34d399", icon: "✎" },
  think: { label: "thinking", color: "#c4b5fd", icon: "…" },
  tool: { label: "tool", color: "#fb923c", icon: "⚙" },
  skill: { label: "skill", color: "#f472b6", icon: "◆" },
  guard: { label: "guard", color: "#fbbf24", icon: "⛨" },
  step: { label: "step", color: "#8b94a7", icon: "·" },
  ask: { label: "customer", color: "#fbbf24", icon: "❝" },
};

export const kindStyle = (k: string) => KIND_STYLE[k] ?? KIND_STYLE.step;

export const isCustomer = (a: { kind: string }) => a.kind === "customer";

/** A container span envelopes other spans; its duration is the sum of everyone's work. */
export const isContainer = (e: { kind: string; container?: boolean }) =>
  e.container ?? (e.kind === "turn" || e.kind === "spawn" || e.kind === "delegate");

const MAX_GAP_MS = 400;   // dead air between turns is squeezed to this
const MIN_DUR_MS = 140;   // a 3ms tool call still needs to be seeable
const MAX_DUR_MS = 1600;  // …and a 9s model call must not hold the stage after everyone stopped

/** The office view walks characters between stations — a beat has to outlast the ~700ms walk
 *  or every scene is a teleport. The timeline view keeps the tighter defaults. */
export const OFFICE_PACING = { maxGapMs: 900, minDurMs: 900 };

/**
 * Lay events on a play clock: real gaps longer than maxGapMs collapse (a conversation with a
 * 20s pause between turns would otherwise be 20s of an empty stage), every event is clamped to
 * [minDurMs, maxDurMs] so instant spans stay visible AND one long model call cannot run on
 * after the last beat started (that left the office standing still for seconds), and ordering
 * is preserved.
 *
 * This is a WATCHING clock, not a stopwatch — `realMsAt` maps it back to real trace time, which
 * is what the UI should display so the readout matches the duration on the trace page.
 */
export function toPlayEvents(
  events: ReplayEvent[],
  {
    maxGapMs = MAX_GAP_MS,
    minDurMs = MIN_DUR_MS,
    maxDurMs = MAX_DUR_MS,
  }: { maxGapMs?: number; minDurMs?: number; maxDurMs?: number } = {},
): { events: PlayEvent[]; total: number } {
  const sorted = [...events].sort((a, b) => a.t_ms - b.t_ms);
  const out: PlayEvent[] = [];
  let clock = 0;
  let prevReal = sorted.length ? sorted[0].t_ms : 0;
  sorted.forEach((e, index) => {
    clock += Math.min(Math.max(0, e.t_ms - prevReal), maxGapMs);
    prevReal = e.t_ms;
    const pdur = Math.min(Math.max(e.dur_ms, minDurMs), Math.max(maxDurMs, minDurMs));
    out.push({ ...e, index, pt: clock, pdur });
  });
  // A container BRACKETS events whose gaps were just squeezed, so its real duration no longer
  // fits the play clock (a 16s turn on a 3.5s clock draws a bar 4x the timeline). Remap its
  // end through the same squeeze: between two starts real time advances 1:1 but never past the
  // (already squeezed) next start; after the last start it runs free.
  const playAt = (real: number) => {
    let k = out.length - 1;
    while (k > 0 && out[k].t_ms > real) k--;
    const ceil = k + 1 < out.length ? out[k + 1].pt - out[k].pt : Infinity;
    return out[k].pt + Math.min(Math.max(0, real - out[k].t_ms), ceil);
  };
  // The clock ends when the WORK ends: containers (turn envelopes) span their whole turn's
  // real duration, and counting them left a long dead tail after the last visible beat.
  // Compute it BEFORE remapping containers — clamping durations shortened the work, so a
  // container measured against the old span would overhang the end of the timeline.
  const work = out.filter((e) => !isContainer(e));
  const total = (work.length ? work : out).reduce((m, e) => Math.max(m, e.pt + e.pdur), 0);
  for (const e of out)
    if (isContainer(e))
      e.pdur = Math.max(Math.min(playAt(e.t_ms + e.dur_ms) - e.pt, total - e.pt), minDurMs);
  return { events: out, total };
}

/**
 * The REAL trace timestamp the play head stands on. The play clock squeezes gaps and clamps
 * durations, so it never matches the trace's wall-clock duration — show this instead.
 */
export function realMsAt(events: PlayEvent[], t: number): number {
  let real = 0;
  for (const e of events) {
    if (e.pt > t) break;
    const frac = e.pdur > 0 ? Math.min(1, (t - e.pt) / e.pdur) : 1;
    real = Math.max(real, e.t_ms + e.dur_ms * frac);
  }
  return real;
}

/** Events in flight at play-time `t`. */
export function activeAt(events: PlayEvent[], t: number): PlayEvent[] {
  return events.filter((e) => t >= e.pt && t < e.pt + e.pdur);
}

/** The most recent event at or before `t` for each actor — what that character is "doing". */
export function currentByActor(events: PlayEvent[], t: number): Record<string, PlayEvent> {
  const out: Record<string, PlayEvent> = {};
  for (const e of events) {
    if (e.pt > t) break;
    if (isContainer(e)) continue; // envelopes, not actions
    out[e.actor] = e;
  }
  return out;
}

/** An actor is on stage from its first span onward. They ARRIVE during the replay (dimmed
 *  until then) and stay — dimming everyone again at the end just looked like a bug. */
export function onStage(actor: ReplayActor, events: PlayEvent[], t: number): boolean {
  const first = events.find((e) => e.actor === actor.id);
  return first !== undefined && t >= first.pt;
}

/** Actors ordered so each sub-agent sits directly under its parent. */
export function orderActors(actors: ReplayActor[]): ReplayActor[] {
  const roots = actors.filter((a) => !a.parent);
  const kids = (id: string) => actors.filter((a) => a.parent === id);
  const out: ReplayActor[] = [];
  const walk = (a: ReplayActor) => { out.push(a); kids(a.id).forEach(walk); };
  roots.forEach(walk);
  // orphans (parent outside the window) keep their original position at the end
  for (const a of actors) if (!out.includes(a)) out.push(a);
  return out;
}

export const fmtMs = (ms: number) => (ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`);

/** A span as returned by `/api/trace` — only the fields the detail card reads. */
export type SpanDetail = {
  span_id: string; input?: unknown; output?: unknown; status_message?: string;
  latency_ms?: number; tokens?: number; cost?: number; model_id?: string;
};

/** Pick one span out of a trace payload. Span ids repeat across traces, so callers must fetch
 *  the trace the event belongs to — this only searches within it. */
export function findSpan(spans: SpanDetail[] | undefined, spanId: string): SpanDetail | null {
  return spans?.find((s) => s.span_id === spanId) ?? null;
}

/** Human-readable block for an arbitrary span input/output payload (JSON, string, or null). */
export function payloadText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
      try {
        return JSON.stringify(JSON.parse(trimmed), null, 2);
      } catch {
        return value; // not JSON after all — show it raw rather than throwing
      }
    }
    return value;
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
