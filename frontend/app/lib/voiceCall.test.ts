// The transcript reducer. This is where "a new bubble for every word" came from: xAI re-sends
// `input_audio_transcription.completed` with the CUMULATIVE text several times per utterance
// (verified against a live session), so a snapshot must REPLACE the growing line, while
// OpenAI's `.delta` fragments must APPEND to it.
import { describe, expect, it } from "vitest";
import { applyLine, type VoiceLine } from "./voiceCall";

const text = (l: VoiceLine[]) => l.map((x) => `${x.role}:${x.text}`);

describe("applyLine", () => {
  it("grows ONE bubble from cumulative snapshots", () => {
    let lines: VoiceLine[] = [];
    for (const t of ["Why did my last", "Why did my last conversation fail?"])
      lines = applyLine(lines, "user", t, "replace", false);
    expect(text(lines)).toEqual(["user:Why did my last conversation fail?"]);
  });

  it("grows ONE bubble from incremental deltas", () => {
    let lines: VoiceLine[] = [];
    for (const t of ["Let me ", "look ", "that up."])
      lines = applyLine(lines, "assistant", t, "append", false);
    expect(text(lines)).toEqual(["assistant:Let me look that up."]);
  });

  it("starts a new bubble when the speaker changes", () => {
    let lines = applyLine([], "user", "hi", "replace", false);
    lines = applyLine(lines, "assistant", "hello", "append", false);
    expect(text(lines)).toEqual(["user:hi", "assistant:hello"]);
  });

  it("never writes to a finished line", () => {
    let lines = applyLine([], "user", "first", "replace", true);
    lines = applyLine(lines, "user", "second", "replace", false);
    expect(text(lines)).toEqual(["user:first", "user:second"]);
  });

  it("keeps the text when a done frame arrives empty", () => {
    let lines = applyLine([], "assistant", "spoken answer", "append", false);
    lines = applyLine(lines, "assistant", "", "replace", true);
    expect(text(lines)).toEqual(["assistant:spoken answer"]);
    expect(lines[0].done).toBe(true);
  });

  it("ignores an empty fragment that would open an empty bubble", () => {
    expect(applyLine([], "user", "", "replace", false)).toEqual([]);
  });
});
