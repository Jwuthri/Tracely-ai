// End-to-end over the transcript path: a REAL grok-voice session, captured from a live xAI
// realtime socket fed synthesized speech, replayed through the production `routeEvent` and
// reducer. If the merge rule ever stops matching what a provider actually sends, this fails.
//
// The session it replays is one spoken question and one spoken answer. Anything other than two
// bubbles is the bug this fixture exists for: xAI re-sends `input_audio_transcription.completed`
// with the cumulative text six times, and a `.completed` still arrives after the assistant has
// begun replying.
import { describe, expect, it, vi } from "vitest";
import { routeEvent, type VoiceEvent } from "./voice";
import { applyLine, type VoiceLine } from "./voiceCall";
import captured from "./__fixtures__/xai-voice-session.json";

function replay(events: Record<string, unknown>[]) {
  const lines: { current: VoiceLine[] } = { current: [] };
  const statuses: string[] = [];
  const onEvent = (e: VoiceEvent) => {
    if (e.type === "transcript")
      lines.current = applyLine(lines.current, e.id, e.role, e.text, e.mode);
    if (e.type === "status") statuses.push(e.status);
  };
  const send = vi.fn();
  for (const raw of events) routeEvent(raw, { onEvent } as never, send);
  return { lines: lines.current, statuses, send };
}

describe("a real captured grok-voice session", () => {
  it("renders exactly one bubble per utterance", () => {
    const { lines } = replay(captured as Record<string, unknown>[]);
    expect(lines.map((l) => l.role)).toEqual(["user", "assistant"]);
    expect(lines[0].text).toBe("Why did my last conversation fail? Please look it up for me.");
    expect(lines[1].text.length).toBeGreaterThan(0);
  });

  it("tracks speaking and listening as the turn moves", () => {
    const { statuses } = replay(captured as Record<string, unknown>[]);
    expect(statuses).toContain("listening");
    expect(statuses).toContain("speaking");
  });

  it("answers a function call back on the wire", () => {
    const send = vi.fn();
    const asked: string[] = [];
    routeEvent(
      {
        type: "response.function_call_arguments.done",
        call_id: "call-1",
        arguments: JSON.stringify({ question: "why did it fail" }),
      },
      {
        onEvent: () => {},
        askTracely: async (q: string) => {
          asked.push(q);
          return "It failed the on-topic evaluator.";
        },
      } as never,
      send,
    );
    return new Promise((r) => setTimeout(r, 0)).then(() => {
      expect(asked).toEqual(["why did it fail"]);
      expect(send.mock.calls[0][0]).toMatchObject({
        type: "conversation.item.create",
        item: { type: "function_call_output", call_id: "call-1" },
      });
      expect(send.mock.calls[1][0]).toEqual({ type: "response.create" });
    });
  });
});
