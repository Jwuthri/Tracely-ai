import { describe, expect, it } from "vitest";
import type { EvalScore, SpanOut } from "../../lib/api";
import {
  asRoleMessage,
  assistantText,
  deriveTitle,
  durationMs,
  firstText,
  fmtDateTime,
  fmtMs,
  selfMs,
  fmtScoreValue,
  fmtTokens,
  imageSrc,
  jsonResultLabel,
  lastTurnMessage,
  messageList,
  modelColor,
  msgRole,
  nearestAgentLabel,
  parseMaybe,
  scoreKey,
  toMsg,
} from "./format";

const span = (o: Partial<SpanOut> = {}): SpanOut => ({
  span_id: "s", parent_span_id: "", name: "", type: "SPAN", level: "", status_message: "",
  start_time: "2026-06-14T10:00:00Z", end_time: null, latency_ms: null, agent_id: "", agent_run_id: "",
  turn_id: "", step_name: "", model_id: "", tokens: 0, cost: 0, metadata: {}, input: null, output: null, ...o,
});

describe("fmtMs", () => {
  it("handles null / negative / zero / sub-ms / ms / s", () => {
    expect(fmtMs(null)).toBe("—");
    expect(fmtMs(-1)).toBe("—");
    expect(fmtMs(0)).toBe("<1ms");
    expect(fmtMs(0.5)).toBe("0.50ms");
    expect(fmtMs(250)).toBe("250ms");
    expect(fmtMs(1500)).toBe("1.50s");
  });
});

describe("fmtTokens", () => {
  it("scales to k / M", () => {
    expect(fmtTokens(900)).toBe("900");
    expect(fmtTokens(1500)).toBe("1.5k");
    expect(fmtTokens(2_000_000)).toBe("2.0M");
  });
});

describe("fmtDateTime", () => {
  it("returns '' for empty / invalid", () => {
    expect(fmtDateTime(null)).toBe("");
    expect(fmtDateTime("not-a-date")).toBe("");
  });
  it("formats a valid ISO timestamp", () => {
    expect(fmtDateTime("2026-06-14T10:05:09Z")).toMatch(/2026-06-14 \d\d:05:09/);
  });
});

describe("durationMs", () => {
  it("prefers latency_ms", () => expect(durationMs(span({ latency_ms: 42 }))).toBe(42));
  it("falls back to end-start", () =>
    expect(durationMs(span({ start_time: "2026-06-14T10:00:00Z", end_time: "2026-06-14T10:00:02Z" }))).toBe(2000));
  it("returns null when unknown", () => expect(durationMs(span())).toBeNull());
});

describe("firstText", () => {
  it("plucks the user turn from a chat array (over system/tool)", () => {
    const v = [{ role: "system", content: "you are a bot" }, { role: "user", content: "where is my order?" }];
    expect(firstText(v)).toBe("where is my order?");
  });
  it("unwraps {messages:[...]} and {question:...} envelopes", () => {
    expect(firstText({ messages: [{ role: "user", content: "hi" }] })).toBe("hi");
    expect(firstText({ question: "the q" })).toBe("the q");
  });
  it("reads content-block arrays", () => {
    expect(firstText([{ type: "text", text: "block text" }])).toBe("block text");
  });
});

describe("deriveTitle", () => {
  it("defaults to 'Conversation' for empty", () => expect(deriveTitle(null)).toBe("Conversation"));
  it("unwraps a JSON message envelope to the user text", () => {
    expect(deriveTitle('{"messages":[{"role":"user","content":"Where is my order?"}]}')).toBe("Where is my order?");
  });
  it("returns 'Conversation' when JSON parses but has no text (LangGraph empty root)", () => {
    expect(deriveTitle('[{"role":"user","content":""}]')).toBe("Conversation");
  });
  it("uses the first non-empty line (CrewAI '\\nCurrent Task:' prefix)", () => {
    expect(deriveTitle("\nCurrent Task: do the thing")).toBe("Current Task: do the thing");
  });
  it("truncates to 7 words with an ellipsis", () => {
    expect(deriveTitle("one two three four five six seven eight nine")).toBe("one two three four five six seven…");
  });
});

describe("parseMaybe", () => {
  it("parses JSON, passes plain strings through, null-safe", () => {
    expect(parseMaybe('{"a":1}')).toEqual({ a: 1 });
    expect(parseMaybe("plain")).toBe("plain");
    expect(parseMaybe("{bad json")).toBe("{bad json");
    expect(parseMaybe(null)).toBeNull();
  });
});

describe("msgRole", () => {
  it("normalizes LangChain human/ai to user/assistant", () => {
    expect(msgRole({ type: "human" })).toBe("user");
    expect(msgRole({ type: "ai" })).toBe("assistant");
    expect(msgRole({ role: "tool" })).toBe("tool");
  });
});

describe("messageList", () => {
  it("returns the messages of a {messages:[...]} wrapper or a bare chat array", () => {
    expect(messageList({ messages: [{ role: "user", content: "x" }] })).toHaveLength(1);
    expect(messageList([{ role: "user", content: "x" }])).toHaveLength(1);
  });
  it("returns null for non-message shapes", () => {
    expect(messageList("plain")).toBeNull();
    expect(messageList([{ foo: 1 }])).toBeNull();
  });
});

describe("lastTurnMessage", () => {
  const raw = JSON.stringify([
    { role: "user", content: "q1" },
    { role: "assistant", content: "a1" },
    { role: "user", content: "q2" },
  ]);
  it("returns the LAST message of the requested role", () => {
    expect(lastTurnMessage(raw, "user")?.content).toBe("q2");
    expect(lastTurnMessage(raw, "assistant")?.content).toBe("a1");
  });
  it("undefined for non-list, null for a list missing that side", () => {
    expect(lastTurnMessage("plain", "user")).toBeUndefined();
    expect(lastTurnMessage(JSON.stringify([{ role: "user", content: "q" }]), "assistant")).toBeNull();
  });
});

describe("asRoleMessage", () => {
  it("wraps a {question:...} kwarg dict into a {role,content} message", () => {
    expect(asRoleMessage("user", '{"question":"hi"}')).toBe('{"role":"user","content":"hi"}');
  });
  it("passes through values that already look like messages", () => {
    const m = '{"role":"user","content":"hi"}';
    expect(asRoleMessage("user", m)).toBe(m);
  });
  it("is null/empty-safe", () => expect(asRoleMessage("user", "")).toBe(""));
});

describe("assistantText", () => {
  it("takes the last assistant turn from a chat array", () => {
    const v = [{ role: "user", content: "q" }, { role: "assistant", content: "the answer" }];
    expect(assistantText(v)).toBe("the answer");
  });
});

describe("toMsg", () => {
  it("unwraps a single-element [{role,content}] array", () => {
    expect(toMsg("assistant", '[{"role":"assistant","content":"hi"}]')).toEqual({ role: "assistant", content: "hi" });
  });
  it("pulls this side's text from a kwarg dict", () => {
    expect(toMsg("user", '{"question":"where?"}')).toEqual({ role: "user", content: "where?" });
  });
  it("returns null for empty", () => expect(toMsg("user", null)).toBeNull());
});

describe("scoreKey", () => {
  const s = (o: Partial<EvalScore>): EvalScore => ({
    name: "faith", evaluation_level: "AGENT_RUN", observation_id: "", value: null, verdict: "", comment: "", data_type: "BOOLEAN", ...o,
  });
  it("keys by span / thread / trace in priority order", () => {
    expect(scoreKey(s({ observation_id: "sp1" }))).toBe("span:sp1|faith");
    expect(scoreKey(s({ evaluation_level: "CONVERSATION", session_id: "th1" }))).toBe("th:th1|faith");
    expect(scoreKey(s({ trace_id: "tr1" }))).toBe("tr:tr1|faith");
    expect(scoreKey(s({ evaluation_level: "CONVERSATION" }))).toBeNull();
  });
});

describe("jsonResultLabel", () => {
  it("headlines a short label field, skipping prose", () => {
    expect(jsonResultLabel('{"reason":"a long explanation here","intent":"refund"}')).toBe("refund");
  });
  it("null for pure-score objects / non-JSON", () => {
    expect(jsonResultLabel('{"score":0.9}')).toBeNull();
    expect(jsonResultLabel("not json")).toBeNull();
  });
});

describe("fmtScoreValue", () => {
  const s = (o: Partial<EvalScore>): EvalScore => ({
    name: "m", evaluation_level: "AGENT_RUN", observation_id: "", value: null, verdict: "", comment: "", data_type: "NUMERIC", ...o,
  });
  it("blanks BOOLEAN (the chip shows it)", () => expect(fmtScoreValue(s({ data_type: "BOOLEAN" }))).toBe(""));
  it("formats latency specially", () => expect(fmtScoreValue(s({ name: "x.latency_ms", value: 1500 }))).toBe("1.50s"));
  it("rounds floats to 2dp, keeps ints", () => {
    expect(fmtScoreValue(s({ value: 0.3333 }))).toBe("0.33");
    expect(fmtScoreValue(s({ value: 4 }))).toBe("4");
  });
});

describe("modelColor", () => {
  it("maps families to theme tokens", () => {
    expect(modelColor("gpt-4o")).toContain("text-ok");
    expect(modelColor("claude-haiku-4-5")).toContain("text-t_tool");
    expect(modelColor("mystery-model")).toContain("text-fg");
  });
});

describe("nearestAgentLabel", () => {
  it("walks up to the nearest AGENT ancestor", () => {
    const all = [
      span({ span_id: "agent", type: "AGENT", name: "support-agent" }),
      span({ span_id: "gen", parent_span_id: "agent", type: "GENERATION" }),
    ];
    expect(nearestAgentLabel(all[1], all)).toBe("support-agent");
  });
  it("returns own name when the span IS an agent", () => {
    const a = span({ type: "AGENT", name: "billing-agent" });
    expect(nearestAgentLabel(a, [a])).toBe("billing-agent");
  });
  it("prefers a declared slug over a framework hop name", () => {
    // LangGraph names every handoff `agent_teams.delegate`; the specialist is in the slug.
    const all = [
      span({ span_id: "d", type: "AGENT", name: "agent_teams.delegate", metadata: { "tracely.agent.id": "collections" } }),
      span({ span_id: "gen", parent_span_id: "d", type: "GENERATION" }),
    ];
    expect(nearestAgentLabel(all[0], all)).toBe("collections");
    expect(nearestAgentLabel(all[1], all)).toBe("collections");
  });
  it("trusts a declared slug over the ancestor chain", () => {
    // The instrumentor parents a sub-agent's spans by the framework run tree, so they can hang off
    // the trace root while the delegate that entered them is a sibling. The slug is the truth.
    const all = [
      span({ span_id: "turn", type: "AGENT", name: "agent_teams.turn", metadata: { "tracely.agent.id": "support-team" } }),
      span({ span_id: "gen", parent_span_id: "turn", type: "GENERATION", metadata: { "tracely.agent.id": "collections" } }),
    ];
    expect(nearestAgentLabel(all[1], all)).toBe("collections");
  });
  it("ignores an inherited slug — it describes the trace, not the span", () => {
    const a = span({ type: "AGENT", name: "support-agent", metadata: { "tracely.agent.id.inherited": "default" } });
    expect(nearestAgentLabel(a, [a])).toBe("support-agent");
  });
});

describe("nearestAgentLabel inside an eval recording", () => {
  const span = (over: Partial<SpanOut>): SpanOut =>
    ({
      span_id: "s1", parent_span_id: "", name: "openai/gpt-5", type: "GENERATION",
      level: "DEFAULT", status_message: "", start_time: "", end_time: null, latency_ms: null,
      agent_id: "", agent_run_id: "", turn_id: "", step_name: "", model_id: "",
      tokens: 0, cost: 0, metadata: {}, input: null, output: null, ...over,
    }) as SpanOut;

  it("names the evaluator column, since a recording has no agent", () => {
    const s = span({
      metadata: {
        "tracely.metadata.evaluator": "tracely.run.quality",
        "tracely.metadata.level": "AGENT_RUN",
      },
    });
    expect(nearestAgentLabel(s, [s])).toBe("tracely.run.quality");
  });

  it("leaves a real agent span alone", () => {
    const s = span({ metadata: { "tracely.agent.id": "planner" } });
    expect(nearestAgentLabel(s, [s])).toBe("planner");
  });
});

describe("imageSrc", () => {
  it("reads the provider image shapes", () => {
    expect(imageSrc({ type: "image_url", image_url: { url: "https://x/a.png" } })).toBe("https://x/a.png");
    expect(imageSrc({ type: "image_url", image_url: "data:image/png;base64,AAAA" })).toBe("data:image/png;base64,AAAA");
    expect(imageSrc({ type: "image", source: { type: "base64", media_type: "image/jpeg", data: "A".repeat(20) } })).toBe(
      `data:image/jpeg;base64,${"A".repeat(20)}`,
    );
    expect(imageSrc({ type: "image", source: { type: "url", url: "https://x/b.jpg" } })).toBe("https://x/b.jpg");
    expect(imageSrc({ type: "image", image: { url: "https://x/c.jpg" } })).toBe("https://x/c.jpg");
    expect(imageSrc({ type: "image" })).toBeNull();
    expect(imageSrc({ type: "image", image_url: { url: "javascript:alert(1)" } })).toBeNull();
  });
});

// ── self time ──────────────────────────────────────────────────────────────────
// The reason this exists: `tracely.thinking()` wrapped around an LLM call yields a THINKING span
// and a GENERATION span with identical start and duration. Wall time can't say which one spent
// the time; self time can.
describe("selfMs", () => {
  const at = (id: string, parent: string, startMs: number, endMs: number): SpanOut =>
    ({ ...span(), span_id: id, parent_span_id: parent,
       start_time: new Date(startMs).toISOString(), end_time: new Date(endMs).toISOString(),
       latency_ms: endMs - startMs });

  it("returns null when the span has no children", () => {
    const s = at("a", "", 0, 3420);
    expect(selfMs(s, [s])).toBeNull();
  });

  it("reports ~0 for a wrapper whose child covers the whole window", () => {
    const parent = at("think", "", 0, 3420);
    const child = at("gen", "think", 0, 3420);
    expect(selfMs(parent, [parent, child])).toBe(0);
  });

  it("subtracts only the nested time", () => {
    const parent = at("a", "", 0, 1000);
    const child = at("b", "a", 200, 600);
    expect(selfMs(parent, [parent, child])).toBe(600);
  });

  it("unions overlapping children instead of summing them", () => {
    // Two concurrent 400ms calls that overlap: 300–700 and 500–900 cover 600ms, not 800ms.
    const parent = at("a", "", 0, 1000);
    const c1 = at("b", "a", 300, 700);
    const c2 = at("c", "a", 500, 900);
    expect(selfMs(parent, [parent, c1, c2])).toBe(400);
  });

  it("counts a gap between children as the parent's own time", () => {
    const parent = at("a", "", 0, 1000);
    const c1 = at("b", "a", 100, 300);
    const c2 = at("c", "a", 700, 900);
    expect(selfMs(parent, [parent, c1, c2])).toBe(600);
  });

  it("never goes negative when a child's clock overruns its parent", () => {
    const parent = at("a", "", 0, 500);
    const child = at("b", "a", 0, 900); // skewed clock from another process
    expect(selfMs(parent, [parent, child])).toBe(0);
  });

  it("ignores grandchildren — only direct children are nested time", () => {
    const parent = at("a", "", 0, 1000);
    const child = at("b", "a", 100, 400);
    const grandchild = at("c", "b", 150, 380);
    expect(selfMs(parent, [parent, child, grandchild])).toBe(700);
  });
});
