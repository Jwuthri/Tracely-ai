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

/** One bubble. `id` is the provider's conversation-item id — the only reliable identity for
 *  "this is still the same utterance" (see `VoiceEvent` in voice.ts). */
export type VoiceLine = { id: string; role: "user" | "assistant"; text: string };

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

/** Grow the transcript by one fragment. Every fragment carrying a given `id` updates that ONE
 *  bubble in place: `replace` carries the whole utterance so far, `append` only what is new.
 *
 *  Keyed by id rather than "the last line, if the speaker matches", because a real microphone
 *  makes server VAD re-fire `speech_started` between words — which used to split one sentence
 *  into a bubble per fragment, each repeating the last. The item id doesn't move. */
export function applyLine(
  lines: VoiceLine[],
  id: string,
  role: "user" | "assistant",
  text: string,
  mode: "append" | "replace",
): VoiceLine[] {
  const i = lines.findIndex((l) => l.id === id);
  if (i === -1) return text ? [...lines, { id, role, text }] : lines;
  const cur = lines[i];
  // An empty `.done` (some providers send one) closes the utterance without blanking it.
  const text_ = mode === "append" ? cur.text + text : text || cur.text;
  const next = lines.slice();
  next[i] = { ...cur, text: text_ };
  return next;
}

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
        if (e.type === "transcript")
          return set({ lines: applyLine(state.lines, e.id, e.role, e.text, e.mode) });
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
  set({ status: "closed" });
}
