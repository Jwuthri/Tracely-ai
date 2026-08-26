// The transcript reducer — where "a new bubble for every word" came from.
//
// Two provider behaviours conspire: xAI re-sends `input_audio_transcription.completed` with the
// CUMULATIVE text several times per utterance, and a real microphone makes server VAD re-fire
// `speech_started` between words. Grouping by "the last line, if the speaker matches" split one
// sentence into a bubble per fragment, each repeating the one before. Grouping by the provider's
// own `item_id` doesn't care about either.
//
// The xAI sequence below is REAL — captured from a live grok-voice session fed synthesized
// speech — so this test fails if the merge rule stops matching what the provider actually sends.
import { describe, expect, it } from "vitest";
import { applyLine, type VoiceLine } from "./voiceCall";

const USER = "item-user-1";
const BOT = "item-bot-1";
const show = (l: VoiceLine[]) => l.map((x) => `${x.role}:${x.text}`);

describe("applyLine", () => {
  it("keeps ONE bubble across xAI's repeated cumulative snapshots", () => {
    // verbatim from the captured session, `.updated` and `.completed` interleaved
    const snapshots = [
      "Why did my last",
      "Why did my last",
      "Why did my last conversation fail?",
      "Why did my last conversation fail?",
      "Why did my last conversation fail? Please look it up for me.",
      "Why did my last conversation fail? Please look it up for me.",
      "Why did my last conversation fail? Please look it up for me.",
    ];
    let lines: VoiceLine[] = [];
    for (const t of snapshots) lines = applyLine(lines, USER, "user", t, "replace");
    expect(show(lines)).toEqual(["user:Why did my last conversation fail? Please look it up for me."]);
  });

  it("keeps ONE bubble across OpenAI's incremental deltas", () => {
    let lines: VoiceLine[] = [];
    for (const t of ["Let me ", "look ", "that up."])
      lines = applyLine(lines, BOT, "assistant", t, "append");
    expect(show(lines)).toEqual(["assistant:Let me look that up."]);
  });

  it("starts a new bubble per utterance, not per fragment", () => {
    let lines = applyLine([], USER, "user", "first question", "replace");
    lines = applyLine(lines, BOT, "assistant", "an answer", "append");
    lines = applyLine(lines, "item-user-2", "user", "second question", "replace");
    expect(show(lines)).toEqual([
      "user:first question",
      "assistant:an answer",
      "user:second question",
    ]);
  });

  it("still updates an earlier bubble when a late fragment arrives out of order", () => {
    // xAI sends one last `.completed` for the user AFTER the assistant has started speaking.
    let lines = applyLine([], USER, "user", "why did it fail", "replace");
    lines = applyLine(lines, BOT, "assistant", "Looking…", "append");
    lines = applyLine(lines, USER, "user", "why did it fail exactly", "replace");
    expect(show(lines)).toEqual(["user:why did it fail exactly", "assistant:Looking…"]);
  });

  it("keeps the text when an empty done frame arrives", () => {
    let lines = applyLine([], BOT, "assistant", "spoken answer", "append");
    lines = applyLine(lines, BOT, "assistant", "", "replace");
    expect(show(lines)).toEqual(["assistant:spoken answer"]);
  });

  it("ignores an empty fragment that would open an empty bubble", () => {
    expect(applyLine([], USER, "user", "", "replace")).toEqual([]);
  });
});
