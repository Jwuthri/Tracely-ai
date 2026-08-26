// The live voice call, held OUTSIDE the React tree.
//
// A call must outlive the component showing it: the user closes the panel to look at a trace,
// flips to the chat view, navigates to another page — and keeps talking. So the session, its
// transcript and its status live in this module singleton and components subscribe with
// `useSyncExternalStore`. Unmounting `VoiceMode` no longer hangs up; only `endCall()` does
// (and a full page reload, which no WebRTC or WebSocket survives).
//
// Same store shape as the live eval scores: one immutable snapshot, replaced on every change.

import { startVoiceSession, type VoiceHandle, type VoiceStatus } from "./voice";

export type VoiceLine = { role: "user" | "assistant"; text: string; done: boolean };

export type VoiceCallState = {
  status: VoiceStatus;
  lines: VoiceLine[];
  error: string;
  /** Which provider/voice is on the wire — a call keeps its own, whatever the pickers now say. */
  provider: string;
  voice: string;
};

const IDLE: VoiceCallState = { status: "closed", lines: [], error: "", provider: "", voice: "" };

let state: VoiceCallState = IDLE;
let handle: VoiceHandle | null = null;
const subscribers = new Set<() => void>();

function set(patch: Partial<VoiceCallState>) {
  state = { ...state, ...patch };
  subscribers.forEach((fn) => fn());
}

export function subscribeVoiceCall(fn: () => void): () => void {
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
  };
}

export function getVoiceCall(): VoiceCallState {
  return state;
}

/** The server render (and the first client render) always sees an idle call. */
export function getVoiceCallServerSnapshot(): VoiceCallState {
  return IDLE;
}

export function isCallActive(s: VoiceCallState = state): boolean {
  return s.status !== "closed";
}

/** Grow the transcript by one fragment. Consecutive fragments from the same speaker are ONE
 *  bubble that updates in place — a `replace` carries the whole utterance so far, an `append`
 *  carries only what is new. A finished line is never written to again. */
export function applyLine(
  lines: VoiceLine[],
  role: "user" | "assistant",
  text: string,
  mode: "append" | "replace",
  final: boolean,
): VoiceLine[] {
  const last = lines[lines.length - 1];
  if (last && last.role === role && !last.done) {
    const merged = { role, text: mode === "append" ? last.text + text : text, done: final };
    // A `done` frame with nothing in it (some providers send an empty `.done`) closes the line
    // rather than blanking it.
    if (!merged.text && last.text) merged.text = last.text;
    return [...lines.slice(0, -1), merged];
  }
  return text ? [...lines, { role, text, done: final }] : lines;
}

const closeOpenLines = (lines: VoiceLine[]): VoiceLine[] =>
  lines.some((l) => !l.done) ? lines.map((l) => (l.done ? l : { ...l, done: true })) : lines;

export async function startCall(opts: {
  provider: "openai" | "xai";
  voice: string;
  askTracely: (question: string) => Promise<string>;
}): Promise<void> {
  if (handle) return;
  set({ status: "connecting", lines: [], error: "", provider: opts.provider, voice: opts.voice });
  try {
    handle = await startVoiceSession({
      provider: opts.provider,
      voice: opts.voice,
      askTracely: opts.askTracely,
      onEvent: (e) => {
        if (e.type === "status") {
          // `closed` arrives both from our own `stop()` and from the provider dropping the
          // socket; either way the handle is spent.
          if (e.status === "closed") handle = null;
          return set({ status: e.status });
        }
        if (e.type === "error") return set({ error: e.detail });
        if (e.type === "turn") return set({ lines: closeOpenLines(state.lines) });
        if (e.type === "transcript")
          return set({ lines: applyLine(state.lines, e.role, e.text, e.mode, e.final) });
      },
    });
  } catch (err) {
    handle = null;
    set({
      status: "closed",
      error: err instanceof Error ? err.message : "couldn't start the voice session",
    });
  }
}

export function endCall(): void {
  handle?.stop();
  handle = null;
  set({ status: "closed", lines: closeOpenLines(state.lines) });
}
