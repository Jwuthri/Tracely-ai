// Pure geometry + pose engine for the Fleet office (tested — no React, no fetch).
//
// The office is a 100×100 coordinate space (percent of the stage). layoutOffice seats every
// actor; poseAt answers, for one actor at play-time t: WHERE they stand, what they're DOING,
// and what floats above their head. The component only animates between poses.

import { isContainer, isCustomer, type PlayEvent, type ReplayActor } from "./timeline";

export type Pt = { x: number; y: number };

export type OfficeLayout = {
  desks: Record<string, Pt>;
  library: Pt; // stand point in front of the bookshelf
  tools: Pt;   // stand point at the tool wall
  door: Pt;    // where characters enter from
  customer: Pt; // the customer's side of the reception counter
  greet: Pt;    // the staff side of the counter, where the supervisor takes the question
  coffee: Pt;
  /** Stand points in the break room (bottom-left): coffee corner, pong sides, watercooler, couch. */
  breaks: { coffee: Pt; pongA: Pt; pongB: Pt; water: Pt; couch: Pt };
};

/** `faded`: a last word that has been up for a while — still readable, visually quieter.
 *  A chip's `sub` is what the tool returned, for a turn that ends on a tool run. */
export type Bubble = (
  | { type: "speech"; text: string }
  | { type: "thought"; text: string }
  | { type: "chip"; icon: "tool" | "skill" | "call"; text: string; sub?: string }
  | { type: "error"; text: string }
) & {
  faded?: boolean;
  /** 0..1 — how much of the text is "typed" so far. Only set while the beat is in flight;
   *  the renderer reveals `ceil(len·progress)` characters. Absent = fully written. */
  progress?: number;
};

export type Pose = {
  x: number;
  y: number;
  at: "desk" | "library" | "tools" | "counter" | "break";
  action: PlayEvent | null;
  bubble: Bubble | null;
  facing: 1 | -1;
  entered: boolean;
  working: boolean;
  /** The customer only: how this turn is going for them. */
  mood?: "happy" | "grumpy" | null;
};

/** One idle agent's assignment in the break room (see `breakPlan`). */
export type BreakSpot = Pt & {
  kind: "coffee" | "pong-a" | "pong-b" | "water" | "couch";
  facing: 1 | -1;
  line: string;
};

/** Seat roots in a row across the floor, sub-agents on a lower row clustered near their
 *  parent. Fixed furniture hugs the walls. All positions are % of the stage. */
export function layoutOffice(all: ReplayActor[]): OfficeLayout {
  const actors = all.filter((a) => !isCustomer(a)); // the customer has no desk — they stand by the door
  const roots = actors.filter((a) => !a.parent);
  const desks: Record<string, Pt> = {};
  const rootY = 40;
  const subY = 66;
  // Roots stop short of the right corner: that is the customer's spot, and their question
  // hangs leftward from it in the same band a root's bubble rises into.
  roots.forEach((r, i) => {
    const x = roots.length === 1 ? 46 : 20 + (i * 48) / Math.max(1, roots.length - 1);
    desks[r.id] = { x, y: rootY };
  });
  // subs cluster under their parent; siblings fan out around the parent's x
  const byParent = new Map<string, ReplayActor[]>();
  for (const a of actors) {
    if (a.parent) byParent.set(a.parent, [...(byParent.get(a.parent) ?? []), a]);
  }
  for (const [pid, kids] of byParent) {
    const px = desks[pid]?.x ?? 50;
    // step shrinks with the sibling count so a big team fans out instead of clamping into a
    // pile at the floor's edge; deeper generations drop a row. Lower rows start at x=30:
    // the bottom-left corner is the break room now, and a desk in it reads as a bug.
    const step = Math.min(18, 60 / Math.max(1, kids.length - 1));
    kids.forEach((k, i) => {
      const spread = kids.length === 1 ? 0 : (i - (kids.length - 1) / 2) * step;
      desks[k.id] = { x: clamp(px + spread, 30, 86), y: Math.min(subY + (k.depth - 1) * 11, 84) };
    });
  }
  // orphans (parent outside the window) get root seating at the end, wrapping to new rows
  let extra = 0;
  for (const a of actors) {
    if (!desks[a.id]) {
      desks[a.id] = { x: 30 + (extra % 4) * 16, y: rootY + Math.floor(extra / 4) * 13 };
      extra++;
    }
  }
  return {
    desks,
    library: { x: 8.5, y: 48 },
    tools: { x: 91.5, y: 52 },
    door: { x: 88, y: 22 },
    customer: { x: 87, y: 31 },
    greet: { x: 78, y: 33 },
    coffee: { x: 8, y: 84 },
    breaks: {
      coffee: { x: 9.5, y: 72 },
      pongA: { x: 7, y: 90 },
      pongB: { x: 22.5, y: 90 },
      water: { x: 24, y: 72 },
      couch: { x: 3.5, y: 85 },
    },
  };
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/** How long a finished event reads as FRESH (ms of play time). A last word stays up until the
 *  actor acts again — it just fades after this. */
const LINGER = 1600;

/** What an actor shows for ONE beat — in flight or finished, the same rendering either way.
 *  An llm is the interesting case: a model that answered with a tool call has no words, and
 *  showing "…" for it (the old behaviour) meant a supervisor stood there saying nothing for
 *  most of the conversation. Order: failure, handoff, words, the tools it called, the model. */
export function wordOf(e: PlayEvent, events: PlayEvent[] = []): Bubble | null {
  if (e.status === "error") return { type: "error", text: e.name };
  // a handoff is a phone call now — the ☎ marks it in the bubble AND the transcript
  if (e.delegate_to || e.kind === "delegate") return { type: "speech", text: `☎ ${e.say || `→ ${e.name}`}` };
  if (e.kind === "ask") return { type: "speech", text: e.detail || "…" };
  if (e.kind === "tool" || e.kind === "skill")
    return { type: "chip", icon: e.kind === "skill" ? "skill" : "tool", text: e.name, sub: e.detail };
  if (e.kind === "think") return e.detail ? { type: "thought", text: e.detail } : null;
  if (e.kind !== "llm") return null;
  const calls = e.calls ?? [];
  // `detail` is "→ a, b" when the model only called tools — the chip says that better.
  const spoke = e.detail && !(calls.length && e.detail.startsWith("→")) ? e.detail : "";
  if (spoke) return { type: "speech", text: spoke };
  if (calls.length) return { type: "chip", icon: "call", text: calls.join(", ") };
  // Nothing recorded on the span itself. ONLY the actor's last beat of the turn may borrow the
  // turn envelope's answer (frameworks record the final reply on the graph root): letting every
  // silent llm borrow it made each of them repeat the conversation's last message.
  const isLastOfTurn = !events.some(
    (o) => !isContainer(o) && o.actor === e.actor && o.trace_id === e.trace_id && o.pt > e.pt,
  );
  const owned = isLastOfTurn
    ? events.find((c) => isContainer(c) && c.actor === e.actor && c.trace_id === e.trace_id && c.detail)?.detail
    : "";
  if (owned) return { type: "speech", text: owned };
  return e.model ? { type: "chip", icon: "call", text: `✎ ${e.model}` } : null;
}

/** Play-time each of an actor's beats OWNS the bubble. Beats are laid end to end in order,
 *  each holding the head for at least MIN_DWELL: prod traces fire an llm and the tool it just
 *  asked for ~1ms apart, and since the newer beat masks the older one, the supervisor's
 *  decision flashed for a millisecond and was gone. A beat may lag its own start by at most
 *  MAX_LAG so a burst of spans can't drift the bubble far from what the character is doing;
 *  the last beat holds indefinitely (that is the "last word stays up" rule). */
const MIN_DWELL = 650;
const MAX_LAG = 1300;

export function bubbleWindows(actorId: string, events: PlayEvent[]): { e: PlayEvent; from: number }[] {
  const out: { e: PlayEvent; from: number }[] = [];
  for (const e of events) {
    if (e.actor !== actorId || isContainer(e)) continue;
    const prev = out[out.length - 1];
    const from = prev ? Math.min(Math.max(e.pt, prev.from + MIN_DWELL), e.pt + MAX_LAG) : e.pt;
    out.push({ e, from });
  }
  return out;
}

/** The beat whose bubble is on screen at t (see `bubbleWindows`). */
export function bubbleAt(actorId: string, events: PlayEvent[], t: number): PlayEvent | null {
  const windows = bubbleWindows(actorId, events);
  let found: PlayEvent | null = null;
  for (const w of windows) {
    if (w.from > t) break;
    found = w.e;
  }
  return found;
}

/** An actor's last beat at or before t, optionally within one turn. Ordered by START, like the
 *  bubble queue and like the conversation itself: the message an agent produced last is the one
 *  it began last, even when an earlier, longer call finished after it. */
export function lastEventOf(actorId: string, events: PlayEvent[], t: number, traceId?: string): PlayEvent | null {
  let last: PlayEvent | null = null;
  for (const e of events) {
    if (e.pt > t) break;
    if (e.actor !== actorId || isContainer(e) || (traceId !== undefined && e.trace_id !== traceId)) continue;
    last = e;
  }
  return last;
}

/** One row per turn: what the customer asked and every agent's last word IN that turn — the
 *  transcript strip. A sub-agent invoked in a turn shows its last word for that invocation. */
export type TurnDigest = {
  trace_id: string; pt: number; ask: string; words: { actor: string; bubble: Bubble }[];
};
export function turnDigest(events: PlayEvent[], actors: ReplayActor[]): TurnDigest[] {
  const turns: TurnDigest[] = [];
  const byTrace = new Map<string, TurnDigest>();
  for (const e of events) {
    let turn = byTrace.get(e.trace_id);
    if (!turn) {
      turn = { trace_id: e.trace_id, pt: e.pt, ask: "", words: [] };
      byTrace.set(e.trace_id, turn);
      turns.push(turn);
    }
    if (e.kind === "ask") turn.ask = e.detail;
  }
  for (const turn of turns) {
    for (const a of actors) {
      if (isCustomer(a)) continue;
      const last = lastEventOf(a.id, events, Infinity, turn.trace_id);
      let bubble = last ? wordOf(last, events) : null;
      if (!bubble) {
        // Nothing sayable in the turn's beats (a turn ingested as one root span, every child
        // hidden by the type filter, or a turn ending on a silent guard): the reply lives on
        // the agent's own turn envelope.
        let env: PlayEvent | null = null;
        for (const e of events) {
          if (isContainer(e) && e.actor === a.id && e.trace_id === turn.trace_id && e.detail) env = e;
        }
        if (env) bubble = { type: "speech", text: env.detail };
      }
      if (bubble) turn.words.push({ actor: a.id, bubble });
    }
  }
  return turns;
}

/** The most interesting in-flight, non-container event for an actor at t (latest started). */
function inflightOf(actorId: string, events: PlayEvent[], t: number): PlayEvent | null {
  let found: PlayEvent | null = null;
  for (const e of events) {
    if (e.pt > t) break;
    if (e.actor !== actorId || isContainer(e)) continue;
    if (t < e.pt + e.pdur) found = e;
  }
  return found;
}

/** An active handoff FROM this actor: an in-flight llm/tool event with delegate_to, or a
 *  DELEGATE container while the actor has nothing of their own in flight. */
function delegationOf(actorId: string, events: PlayEvent[], t: number): PlayEvent | null {
  let found: PlayEvent | null = null;
  for (const e of events) {
    if (e.pt > t) break;
    if (e.actor !== actorId || !e.delegate_to) continue;
    if (t < e.pt + e.pdur) found = e;
  }
  return found;
}

/** An active handoff TO this actor — their phone is ringing. */
function summonsOf(actorId: string, events: PlayEvent[], t: number): PlayEvent | null {
  let found: PlayEvent | null = null;
  for (const e of events) {
    if (e.pt > t) break;
    if (e.delegate_to !== actorId) continue;
    if (t < e.pt + e.pdur) found = e;
  }
  return found;
}

/** True while a root agent should be at the counter taking the customer's question: the
 *  turn's ask is still in flight and this root does work in that turn. */
function greetingAt(actor: ReplayActor, events: PlayEvent[], t: number): boolean {
  if (isCustomer(actor) || actor.parent || actor.depth) return false;
  let ask: PlayEvent | null = null;
  for (const e of events) { if (e.pt > t) break; if (e.kind === "ask") ask = e; }
  if (!ask || t >= ask.pt + ask.pdur) return false;
  const trace = ask.trace_id;
  // ANY event of theirs counts, container included — a turn recorded as one root span
  // (manual SDK, no child spans) still means this root took the question.
  return events.some((e) => e.trace_id === trace && e.actor === actor.id);
}

/* Break-room policy. A SUB-agent (never the customer, never a root — someone has to mind the
 * counter) is off duty when nothing of theirs is in flight, their last beat settled a while
 * ago (or the day hasn't reached their first task yet), their next beat is far enough away to
 * make the walk worth it, and nobody is ringing their phone. Everything below is a pure
 * function of (events, t) so scrubbing backwards replays identically. */
const BREAK_SETTLE = 2200; // linger at the desk after the last beat before wandering off
const BREAK_LEAD = 1100;   // head back this long before the next beat starts
const BREAK_MIN = 900;     // a window shorter than this isn't worth the walk

const COFFEE_LINES = ["coffee break ☕", "refuel time ☕", "brb — espresso ☕"];
const PONG_LINES = ["quick rally 🏓", "match point 🏓", "my serve 🏓"];
const WATER_LINES = ["staying hydrated 💧", "have you tried turning it off and on?"];
const COUCH_LINES = ["scrolling memes 📱", "five more minutes… 📱"];
const lineHash = (id: string) => {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return h;
};

/** Who is in the break room at t, and where. Two idle agents take the pong table (facing
 *  each other), everyone else queues at the coffee machine. Assignment order follows the
 *  actor list, so it is stable frame to frame. */
export function breakPlan(
  actors: ReplayActor[], events: PlayEvent[], t: number, layout: OfficeLayout,
): Map<string, BreakSpot> {
  const idle: ReplayActor[] = [];
  for (const a of actors) {
    if (isCustomer(a) || !(a.parent || a.depth)) continue;
    if (summonsOf(a.id, events, t)) continue;
    let lastEnd = 0, next = Infinity, started = false;
    for (const e of events) {
      if (e.actor !== a.id || isContainer(e)) continue;
      if (e.pt <= t) { started = true; lastEnd = Math.max(lastEnd, e.pt + e.pdur); }
      else next = Math.min(next, e.pt);
    }
    const from = started ? lastEnd + BREAK_SETTLE : 0;
    const to = next - BREAK_LEAD;
    if (t >= from && t < to && to - from >= BREAK_MIN) idle.push(a);
  }
  const out = new Map<string, BreakSpot>();
  // spot ladder: a pair rallies at the pong table first, then coffee, the watercooler, the
  // couch; overflow queues at the coffee machine. A LONE idle agent gets coffee — playing
  // pong against nobody reads as a bug.
  const ladder: { pt: Pt; kind: BreakSpot["kind"]; facing: 1 | -1; lines: string[] }[] = [
    { pt: layout.breaks.pongA, kind: "pong-a", facing: 1, lines: PONG_LINES },
    { pt: layout.breaks.pongB, kind: "pong-b", facing: -1, lines: PONG_LINES },
    { pt: layout.breaks.coffee, kind: "coffee", facing: -1, lines: COFFEE_LINES },
    { pt: layout.breaks.water, kind: "water", facing: 1, lines: WATER_LINES },
    { pt: layout.breaks.couch, kind: "couch", facing: 1, lines: COUCH_LINES },
  ];
  idle.forEach((a, i) => {
    const h = lineHash(a.id);
    const slot = idle.length === 1 ? ladder[2] : ladder[Math.min(i, ladder.length - 1)];
    const off = i >= ladder.length ? (i - ladder.length + 1) * 5 : 0;
    out.set(a.id, {
      x: slot.pt.x + off, y: slot.pt.y, kind: slot.kind, facing: slot.facing,
      line: slot.lines[h % slot.lines.length],
    });
  });
  return out;
}

/** What an actor's desk phone is doing at t — drives the phone sprite on the desk. */
export function phoneStateAt(actorId: string, events: PlayEvent[], t: number): "idle" | "ringing" | "talking" {
  if (delegationOf(actorId, events, t)) return "talking";
  if (!inflightOf(actorId, events, t) && summonsOf(actorId, events, t)) return "ringing";
  return "idle";
}

/** How long a failure keeps the office alarmed after the erroring beat ended. */
const ALARM_LINGER = 1400;

/** The error beat the office should be alarmed about at t (in flight or just ended). */
export function alarmAt(events: PlayEvent[], t: number): PlayEvent | null {
  let hot: PlayEvent | null = null;
  for (const e of events) {
    if (e.pt > t) break;
    if (e.status !== "error" || isContainer(e)) continue;
    if (t < e.pt + e.pdur + ALARM_LINGER) hot = e;
  }
  return hot;
}

/** A long station beat splits: walk over, GRAB the thing, bring it back to the desk and run
 *  it there. Short beats (most tools) play out entirely at the station — a there-and-back
 *  inside 900ms would be a teleport. */
const GRAB_MS = 650;
const GRAB_SPLIT_MIN = 1400;

/** How far a beat's words have typed out at t (the typewriter effect). */
const TYPE_MS = 900;
const typeProgress = (e: PlayEvent, t: number) =>
  Math.max(0, Math.min(1, (t - e.pt) / Math.min(e.pdur || TYPE_MS, TYPE_MS)));

export function poseAt(
  actor: ReplayActor,
  events: PlayEvent[],
  t: number,
  layout: OfficeLayout,
  slot = 0,
  breakSpot: BreakSpot | null = null,
): Pose {
  if (isCustomer(actor)) {
    // The customer waits at the reception counter the whole time and holds this turn's
    // question up until the next one — the office works for THEM.
    let ask: PlayEvent | null = null;
    for (const e of events) { if (e.pt > t) break; if (e.actor === actor.id) ask = e; }
    const live = ask !== null && t < ask.pt + ask.pdur;
    // mood: sour when the turn failed or drags on, pleased once the team's work is done
    let mood: Pose["mood"] = null;
    if (ask) {
      let lastWorkEnd = -1;
      let anyWork = false;
      let stillWorking = false;
      let failed = false;
      let moreComing = false;
      for (const e of events) {
        if (e.trace_id !== ask.trace_id || isContainer(e) || e.actor === actor.id) continue;
        if (e.pt > t) { moreComing = true; continue; }
        anyWork = true;
        lastWorkEnd = Math.max(lastWorkEnd, e.pt + e.pdur);
        if (t < e.pt + e.pdur) stillWorking = true;
        if (e.status === "error") failed = true;
      }
      mood = failed ? "grumpy"
        : stillWorking && t - ask.pt > 3500 ? "grumpy"
        : anyWork && !stillWorking && !moreComing && t >= lastWorkEnd ? "happy"
        : null;
    }
    return {
      x: layout.customer.x, y: layout.customer.y, at: "counter", action: live ? ask : null,
      bubble: ask ? { ...(wordOf(ask) as Bubble), faded: !live, ...(live ? { progress: typeProgress(ask, t) } : {}) } : null,
      facing: -1, entered: true, working: live, mood,
    };
  }
  const desk = layout.desks[actor.id] ?? { x: 50, y: 50 };
  // The whole team is on the floor from t=0 — idle sub-agents hang out in the break room
  // until the phone rings, then take their own desk.

  const inflight = inflightOf(actor.id, events, t);
  const delegation = delegationOf(actor.id, events, t);
  const summons = inflight ? null : summonsOf(actor.id, events, t);
  const jitter = (slot % 3) * 4 - 4; // stand-point offset so two actors never fully overlap

  // where — the counter beats everything (the supervisor takes the question in person);
  // then stations; a delegator STAYS at their desk and phones instead of walking over.
  let x = desk.x;
  let y = desk.y;
  let at: Pose["at"] = "desk";
  let flavor: Bubble | null = null; // a pose-level line (break room, "on my way") — not a beat
  const station = inflight?.station ?? "desk";
  const backAtDesk = inflight !== null && inflight.pdur >= GRAB_SPLIT_MIN && t >= inflight.pt + GRAB_MS;
  if (greetingAt(actor, events, t)) {
    x = layout.greet.x;
    y = layout.greet.y;
    at = "counter";
  } else if (station === "library" && !backAtDesk) {
    x = layout.library.x + 3;
    y = layout.library.y + jitter;
    at = "library";
  } else if (station === "computer" && !backAtDesk) {
    x = layout.tools.x - 3;
    y = layout.tools.y + jitter;
    at = "tools";
  } else if (summons) {
    flavor = { type: "thought", text: "☎ on my way!" };
  } else if (!inflight && !delegation && breakSpot) {
    x = breakSpot.x;
    y = breakSpot.y;
    at = "break";
    flavor = { type: "thought", text: breakSpot.line };
  }

  // What floats above their head: ONE rule for the whole conversation. `bubbleAt` picks the
  // beat that owns the head right now — in flight or long finished, each beat guaranteed its
  // turn on screen — and `wordOf` renders it. The last beat holds until the actor acts again,
  // fading once it is no longer fresh, so nobody ever stands there with an empty head. A
  // pose-level flavor line (break room, answering the phone) takes the head instead.
  let bubble: Bubble | null = flavor;
  let envBusy = false; // their turn envelope is running and it's ALL they have — still "working"
  if (!bubble) {
    const held = delegation ?? bubbleAt(actor.id, events, t);
    const word = held && wordOf(held, events);
    if (held && word) {
      const ended = held.pt + held.pdur;
      bubble = { ...word, faded: t > ended && t - ended > LINGER };
      // words TYPE OUT while the beat is in flight — a finished beat is fully written
      if (t < ended && (word.type === "speech" || word.type === "thought")) {
        bubble.progress = typeProgress(held, t);
      }
    } else if (!held) {
      // No beats of their own at all (a turn ingested as ONE root span — or every child
      // hidden by the step-type filter): the answer lives on the turn envelope itself.
      // Only when there are NO beats — a silent llm mid-turn must never leak the ending.
      let env: PlayEvent | null = null;
      for (const e of events) {
        if (e.pt > t) break;
        if (isContainer(e) && e.actor === actor.id && e.detail) env = e;
      }
      if (env) {
        const ended = env.pt + env.pdur;
        bubble = { type: "speech", text: env.detail, faded: t > ended && t - ended > LINGER };
        if (t < ended) {
          bubble.progress = typeProgress(env, t);
          envBusy = true;
        }
      }
    }
  }

  return {
    x,
    y,
    at,
    action: inflight,
    bubble,
    facing: at === "break" && breakSpot ? breakSpot.facing : x < desk.x - 1 ? -1 : 1,
    entered: true,
    working: inflight !== null || delegation !== null || envBusy,
  };
}

/** Skills on the shelf: every distinct thing performed at the library. */
export function librarySkills(events: PlayEvent[]): string[] {
  return [...new Set(events.filter((e) => e.station === "library").map((e) => e.name))];
}

/** Tools on the wall: everything actually RUN at the computer first (lit), then the declared
 *  catalog that never ran (dim) — so a big catalog can't evict the tools that did the work. */
export function wallTools(
  events: PlayEvent[],
  declared: DeclaredTool[],
): { name: string; used: boolean }[] {
  const used = [...new Set(events.filter((e) => e.station === "computer").map((e) => e.name))];
  const usedSet = new Set(used);
  const seen = new Set(used);
  return [
    ...used.map((name) => ({ name, used: true })),
    ...declared
      .filter((d) => !usedSet.has(d.name) && !seen.has(d.name) && seen.add(d.name))
      .map((d) => ({ name: d.name, used: false })),
  ];
}

/** A tool as the caller declared it in the agent catalog. */
export type DeclaredTool = { name: string; description: string };

/** Everything the side panel shows for one shelf item — a book at the library, a tool on the
 *  wall. Same idea as the personnel file, for the things instead of the people. */
export type StationInfo = {
  name: string;
  kind: "skill" | "tool";
  description: string;
  runs: number;
  failures: number;
  by: string[];          // actor ids that used it, first use first
  lastResult: string;    // what it returned the last time it ran
  used: boolean;
};

/** Build that card. `description` prefers what the catalog declares; a skill (never declared)
 *  falls back to what it actually returned, so the card is never blank for something that ran. */
export function stationInfo(
  name: string,
  kind: "skill" | "tool",
  events: PlayEvent[],
  declared: DeclaredTool[] = [],
): StationInfo {
  const station = kind === "skill" ? "library" : "computer";
  const runs = events.filter((e) => e.name === name && e.station === station);
  const by: string[] = [];
  for (const e of runs) if (!by.includes(e.actor)) by.push(e.actor);
  const lastResult = [...runs].reverse().find((e) => e.detail)?.detail ?? "";
  const declaredDesc = declared.find((d) => d.name === name)?.description ?? "";
  return {
    name,
    kind,
    description: declaredDesc || lastResult,
    runs: runs.length,
    failures: runs.filter((e) => e.status === "error").length,
    by,
    lastResult,
    used: runs.length > 0,
  };
}

/** The narration line for the LED sign: the latest started event, described. */
export function narrate(
  events: PlayEvent[],
  t: number,
  nameOf: (id: string) => string,
): string {
  // Prefer what is IN FLIGHT right now; once nothing is, speak of the last beat in the past
  // tense — a sign stuck on "X runs Y" seconds after Y finished is lying.
  let current: PlayEvent | null = null;
  let askLeads = false;
  let lastEnded: PlayEvent | null = null;
  let lastEnd = -1;
  for (const e of events) {
    if (e.pt > t) break;
    if (isContainer(e) && !e.delegate_to) continue;
    if (t < e.pt + e.pdur) {
      // the customer's question leads the sign for as long as it is fresh — the agents start
      // working the same instant, and "sup drafts a reply" before anyone heard the ask is odd
      if (!askLeads) current = e;
      askLeads ||= e.kind === "ask";
    } else if (e.pt + e.pdur > lastEnd) {
      lastEnded = e;
      lastEnd = e.pt + e.pdur;
    }
  }
  if (current) {
    const who = nameOf(current.actor);
    if (current.delegate_to) return `${who} ☎ ${nameOf(current.delegate_to)}: ${current.say || "handoff"}`;
    switch (current.kind) {
      case "ask":
        return `customer asks: ${current.detail || "…"}`;
      case "think":
        return `${who} is thinking…`;
      case "skill":
        return `${who} reads «${current.name}»`;
      case "tool":
        return `${who} runs ${current.name}`;
      case "llm":
        // a model turn that answered with a tool call is not "drafting a reply" — say what it
        // actually asked for, the same thing its bubble shows
        return current.calls?.length
          ? `${who} calls ${current.calls.join(", ")}`
          : `${who} drafts a reply (${current.model || "llm"})`;
      default:
        return `${who} · ${current.name}`;
    }
  }
  if (lastEnded) {
    const who = nameOf(lastEnded.actor);
    if (lastEnded.delegate_to) return `${who} → ${nameOf(lastEnded.delegate_to)} · handed off`;
    if (lastEnded.kind === "ask") return "customer is waiting…";
    return `${who} · ${lastEnded.name} ✓`;
  }
  return "office opens…";
}
