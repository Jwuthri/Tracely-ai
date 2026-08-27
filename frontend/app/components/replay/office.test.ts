import { describe, expect, it } from "vitest";
import { alarmAt, breakPlan, bubbleAt, layoutOffice, librarySkills, narrate, phoneStateAt, poseAt, stationInfo, turnDigest, wallTools, wordOf } from "./office";
import { OFFICE_PACING, toPlayEvents, type ReplayActor, type ReplayEvent } from "./timeline";

const actor = (id: string, parent = "", depth = 0): ReplayActor =>
  ({ id, name: id, kind: depth ? "subagent" : "agent", parent, depth, first_ms: 0, last_ms: 0, events: 0, errors: 0 }) as ReplayActor;
const customer: ReplayActor = { ...actor("__customer__"), name: "customer", kind: "customer" };

const ev = (t: number, dur: number, a: string, kind: string, name: string, extra: Partial<ReplayEvent> = {}): ReplayEvent => ({
  t_ms: t, dur_ms: dur, actor: a, kind, name, status: "ok", model: "", detail: "",
  span_id: `${a}-${name}-${t}`, trace_id: "t", turn_id: "", station: "desk", delegate_to: "", say: "", ...extra,
});

describe("layoutOffice", () => {
  const actors = [actor("sup"), actor("faq", "sup", 1), actor("audit", "sup", 1)];
  const l = layoutOffice(actors);
  it("keeps every desk on the floor", () => {
    for (const p of Object.values(l.desks)) {
      expect(p.x).toBeGreaterThan(10); expect(p.x).toBeLessThan(90);
      expect(p.y).toBeGreaterThan(20); expect(p.y).toBeLessThan(90);
    }
  });
  it("seats sub-agents on a lower row near their parent", () => {
    expect(l.desks.faq.y).toBeGreaterThan(l.desks.sup.y);
    expect(Math.abs(l.desks.faq.x - l.desks.sup.x)).toBeLessThan(25);
  });
  it("orphan parents don't crash and get a desk", () => {
    const o = layoutOffice([actor("ghost-child", "missing", 1)]);
    expect(o.desks["ghost-child"]).toBeDefined();
  });
});

describe("poseAt", () => {
  const actors = [actor("sup"), actor("faq", "sup", 1)];
  const layout = layoutOffice(actors);
  const script = toPlayEvents([
    ev(0, 400, "sup", "llm", "chat"),
    ev(500, 600, "sup", "llm", "chat2", { delegate_to: "faq", say: "check the warranty", station: "peer" }),
    ev(1200, 300, "faq", "tool", "lookup_kb", { station: "library" }),
    ev(1600, 300, "faq", "tool", "charge", { station: "computer" }),
    ev(2000, 200, "sup", "think", "thinking", { detail: "hmm" }),
  ]).events;

  it("everyone is seated idle at their desk from t=0", () => {
    const p = poseAt(actors[1], script, 100, layout);
    expect(p.entered).toBe(true);
    expect(p.at).toBe("desk");
    expect(p.working).toBe(false);
    expect(p.bubble).toBeNull();
  });
  it("phones the callee on a handoff instead of walking over", () => {
    const p = poseAt(actors[0], script, 700, layout);
    expect(p.at).toBe("desk");
    expect(p.x).toBe(layout.desks.sup.x);
    expect(p.bubble).toMatchObject({ type: "speech", text: "☎ check the warranty", faded: false });
    expect(p.working).toBe(true);
  });
  it("the summoned callee answers the phone at their desk", () => {
    // the delegation is in flight 400..1000 on the play clock; faq's own first beat is at 800
    const p = poseAt(actors[1], script, 700, layout);
    expect(p.at).toBe("desk");
    expect(p.bubble).toEqual({ type: "thought", text: "☎ on my way!" });
  });
  it("reads knowledge at the library, runs actions at the tool wall", () => {
    // play clock: gaps over 400ms are squeezed — library is in flight at pt 800..1100,
    // the computer tool at 1200..1500, thinking at 1600..1800
    expect(poseAt(actors[1], script, 900, layout).at).toBe("library");
    expect(poseAt(actors[1], script, 1300, layout).at).toBe("tools");
  });
  it("thinks in a thought cloud at the desk", () => {
    const p = poseAt(actors[0], script, 1700, layout);
    expect(p.at).toBe("desk");
    expect(p.bubble?.type).toBe("thought");
  });
  it("an llm IN FLIGHT says what it said — never a bare ellipsis", () => {
    // the old bubble was a hardcoded "…" for any running llm, so a supervisor (nearly all llm
    // spans) stood there saying nothing for most of the conversation
    const solo = [actor("sup")];
    const l = layoutOffice(solo);
    const s2 = toPlayEvents([ev(0, 2000, "sup", "llm", "chat", { detail: "Shipped yesterday." })]).events;
    expect(poseAt(solo[0], s2, 500, l).bubble).toMatchObject({ type: "speech", text: "Shipped yesterday.", faded: false });
  });
  it("keeps naming the tool when a turn ENDS on a tool run, not on words", () => {
    // the computer tool runs 1200..1500 on the play clock — just after it, the character used
    // to stand there with an empty head as if it had answered nothing
    expect(poseAt(actors[1], script, 1550, layout).bubble).toEqual({
      type: "chip", icon: "tool", text: "charge", sub: "", faded: false,
    });
  });
  it("idle actors sit at their desk holding their (faded) last word", () => {
    // faq's last beat was the `charge` tool — its name stays over their head, quieter, until
    // they act again; an empty head would read as "never did anything"
    const p = poseAt(actors[1], script, 9000, layout);
    expect(p.at).toBe("desk");
    expect(p.bubble).toEqual({ type: "chip", icon: "tool", text: "charge", sub: "", faded: true });
    expect(p.working).toBe(false);
    // an actor with no events yet has nothing to say
    expect(poseAt(actors[1], script, 100, layout).bubble).toBeNull();
  });
});

it("librarySkills and wallTools collect the right furniture", () => {
  const script = toPlayEvents([
    ev(0, 100, "a", "skill", "refund-flow", { station: "library" }),
    ev(200, 100, "a", "tool", "lookup_kb", { station: "library" }),
    ev(400, 100, "a", "tool", "charge_card", { station: "computer" }),
  ]).events;
  expect(librarySkills(script)).toEqual(["refund-flow", "lookup_kb"]);
  // executed tools come first (lit); declared-but-never-run follow (dim) — a big catalog
  // can never evict the tools that actually ran
  const declared = [{ name: "send_reply", description: "reply to the guest" }, { name: "charge_card", description: "" }];
  expect(wallTools(script, declared)).toEqual([
    { name: "charge_card", used: true },
    { name: "send_reply", used: false },
  ]);
});

it("stationInfo builds the card for a book or a tool on the wall", () => {
  const script = toPlayEvents([
    ev(0, 100, "a", "skill", "refund-flow", { station: "library", detail: "step 1 → step 2" }),
    ev(200, 100, "b", "tool", "charge_card", { station: "computer", status: "error" }),
    ev(400, 100, "a", "tool", "charge_card", { station: "computer", detail: "{\"ok\":true}" }),
  ]).events;
  const declared = [{ name: "charge_card", description: "Charges the card on file." }];

  const tool = stationInfo("charge_card", "tool", script, declared);
  expect(tool).toMatchObject({ runs: 2, failures: 1, by: ["b", "a"], used: true });
  expect(tool.description).toBe("Charges the card on file.");   // the catalog wins
  expect(tool.lastResult).toBe('{"ok":true}');

  // a skill is never in the tool catalog — it describes itself by what it returned
  expect(stationInfo("refund-flow", "skill", script, declared).description).toBe("step 1 → step 2");
  // a declared tool that never ran still has its card
  expect(stationInfo("send_reply", "tool", script, declared)).toMatchObject({ runs: 0, used: false, by: [] });
});

it("narrate describes the current beat", () => {
  const script = toPlayEvents([
    ev(0, 300, "sup", "llm", "chat", { delegate_to: "faq", say: "go", station: "peer" }),
    ev(400, 300, "faq", "tool", "lookup_kb", { station: "library" }),
  ]).events;
  const name = (id: string) => id.toUpperCase();
  expect(narrate(script, 100, name)).toBe("SUP ☎ FAQ: go");
  expect(narrate(script, 500, name)).toBe("FAQ runs lookup_kb");
  // an llm that answered with a call names the tool instead of "drafts a reply"
  const calling = toPlayEvents([ev(0, 300, "sup", "llm", "chat", { calls: ["transfer_to_billing"], model: "gpt-4o" })]).events;
  expect(narrate(calling, 100, name)).toBe("SUP calls transfer_to_billing");
  expect(narrate(script, 5000, name)).toBe("FAQ · lookup_kb ✓"); // past tense once it ended
});

it("afterglow shows what the actor said LAST, not what finished last", () => {
  const actors = [actor("a")];
  const layout = layoutOffice(actors);
  const script = toPlayEvents([
    ev(0, 2000, "a", "llm", "long-early", { detail: "early words" }),
    ev(500, 300, "a", "llm", "short-late", { detail: "the last word" }),
  ]).events;
  // long-early ENDS last (pt0+2000) but short-late is the later message — the conversation
  // reads in start order, so that is the word left on screen
  expect(poseAt(actors[0], script, 2100, layout).bubble).toEqual({ type: "speech", text: "the last word", faded: false });
  expect(poseAt(actors[0], script, 9000, layout).bubble).toEqual({ type: "speech", text: "the last word", faded: true });
  // …and the earlier one still got its own window first
  expect(poseAt(actors[0], script, 100, layout).bubble?.text).toBe("early words");
});


describe("the customer", () => {
  const actors = [customer, actor("sup")];
  const layout = layoutOffice(actors);
  const script = toPlayEvents([
    ev(0, 0, "__customer__", "ask", "asks", { detail: "Where is my order?", trace_id: "t1", station: "door" }),
    ev(0, 400, "sup", "llm", "chat", { detail: "Shipped yesterday.", trace_id: "t1" }),
    ev(3000, 0, "__customer__", "ask", "asks", { detail: "Refund it.", trace_id: "t2", station: "door" }),
    ev(3000, 300, "sup", "tool", "issue_refund", { detail: '{"refund_id":"rf_1"}', trace_id: "t2", station: "computer" }),
  ], OFFICE_PACING).events;

  it("has no desk and waits at the reception counter the whole time", () => {
    expect(layout.desks.__customer__).toBeUndefined();
    expect(layout.desks.sup).toBeDefined();
    for (const t of [0, 500, 9000]) {
      const p = poseAt(customer, script, t, layout);
      expect(p.at).toBe("counter");
      expect(p.x).toBe(layout.customer.x);
    }
  });
  it("the supervisor takes the question at the counter, then goes back to work", () => {
    // t1's ask is live 0..900 (office pacing) — sup greets at the counter while it is
    const during = poseAt(actors[1], script, 100, layout);
    expect(during.at).toBe("counter");
    expect(during.x).toBe(layout.greet.x);
    // long after the last ask ended, sup is back at their desk
    const after = poseAt(actors[1], script, 5000, layout);
    expect(after.at).toBe("desk");
  });
  it("speaks the turn's question and keeps it up (faded) until the next turn", () => {
    expect(poseAt(customer, script, 100, layout).bubble).toMatchObject({ type: "speech", text: "Where is my order?", faded: false });
    // office pacing: every beat is ≥900 and the 3s pause squeezes to 900, so turn 2 starts at
    // play 900 and its ask is live 900..1800 — long after that it is still up, just faded
    const ask2 = script.find((e) => e.trace_id === "t2")!;
    expect(ask2.pt).toBe(900);
    expect(poseAt(customer, script, ask2.pt + 50, layout).bubble).toMatchObject({ type: "speech", text: "Refund it.", faded: false });
    const later = poseAt(customer, script, 9000, layout);
    expect(later.bubble).toEqual({ type: "speech", text: "Refund it.", faded: true });
    expect(later.working).toBe(false);
  });
  it("narrates the ask", () => {
    expect(narrate(script, 10, (id) => id)).toBe("customer asks: Where is my order?");
  });
  it("turnDigest lists the question and each agent's last word per turn", () => {
    expect(turnDigest(script, actors)).toEqual([
      { trace_id: "t1", pt: 0, ask: "Where is my order?", words: [{ actor: "sup", bubble: { type: "speech", text: "Shipped yesterday." } }] },
      { trace_id: "t2", pt: script.find((e) => e.trace_id === "t2")!.pt, ask: "Refund it.",
        words: [{ actor: "sup", bubble: { type: "chip", icon: "tool", text: "issue_refund", sub: '{"refund_id":"rf_1"}' } }] },
    ]);
  });
});

it("wordOf renders each kind of beat", () => {
  const script = toPlayEvents([
    ev(0, 1000, "sup", "turn", "run", { container: true, detail: "Refund of $1,299 issued." }),
    ev(100, 100, "sup", "llm", "chat", { detail: "" }),
    ev(300, 100, "sup", "tool", "issue_refund", { detail: "ok", status: "error" }),
    ev(500, 100, "sub", "think", "thinking", { detail: "hmm" }),
    ev(700, 100, "sup", "guard", "pii", { detail: "clean" }),
  ]).events;
  const [turn, llm, tool, think, guard] = script;
  expect(wordOf(llm, script)).toBeNull();                  // silent AND not the turn's last beat
  expect(wordOf(tool)).toEqual({ type: "error", text: "issue_refund" });                    // failure beats result
  expect(wordOf({ ...tool, status: "ok" })).toEqual({ type: "chip", icon: "tool", text: "issue_refund", sub: "ok" });
  expect(wordOf(think)).toEqual({ type: "thought", text: "hmm" });
  expect(wordOf(guard)).toBeNull();
  expect(turn.container).toBe(true);
});


describe("an llm that answered with a tool call", () => {
  const solo = [actor("sup")];
  const l = layoutOffice(solo);
  const callEv = (extra: Partial<ReplayEvent>) => toPlayEvents([ev(0, 1000, "sup", "llm", "chat", extra)]).events[0];

  it("names the tools it called instead of standing there silent", () => {
    expect(wordOf(callEv({ detail: "→ transfer_to_billing", calls: ["transfer_to_billing"] }))).toEqual({
      type: "chip", icon: "call", text: "transfer_to_billing",
    });
    // words win when the model actually said something
    expect(wordOf(callEv({ detail: "on it", calls: ["get_weather"] }))).toEqual({ type: "speech", text: "on it" });
    // nothing at all: name the model rather than an empty head
    expect(wordOf(callEv({ detail: "", calls: [], model: "gpt-4o" }))).toEqual({ type: "chip", icon: "call", text: "✎ gpt-4o" });
    expect(wordOf(callEv({ detail: "", calls: [], model: "" }))).toBeNull();
  });

  it("shows the call in the office while it runs", () => {
    const script = [callEv({ detail: "→ lookup", calls: ["lookup"] })];
    expect(poseAt(solo[0], script, 300, l).bubble).toEqual({ type: "chip", icon: "call", text: "lookup", faded: false });
  });
});

it("only the turn's LAST silent llm borrows the turn envelope's answer", () => {
  // frameworks record the final reply on the graph root; letting EVERY silent llm borrow it
  // made each of them repeat the conversation's last message
  const script = toPlayEvents([
    ev(0, 2000, "sup", "turn", "graph", { container: true, detail: "Refund issued." }),
    ev(100, 100, "sup", "llm", "step1", { detail: "" }),
    ev(400, 100, "sup", "llm", "step2", { detail: "" }),
  ]).events;
  const [, first, last] = script;
  expect(wordOf(first, script)).toBeNull();                                   // mid-turn: silent
  expect(wordOf(last, script)).toEqual({ type: "speech", text: "Refund issued." });
  // a different turn's envelope is never borrowed
  const other = { ...last, trace_id: "other" };
  expect(wordOf(other, script)).toBeNull();
});

it("a LONG station beat grabs the tool, then runs it back at the desk", () => {
  const solo = [actor("a", "root", 1)];
  const l = layoutOffice(solo);
  const script = toPlayEvents([ev(0, 2000, "a", "tool", "big_export", { station: "computer" })]).events;
  expect(script[0].pdur).toBeGreaterThanOrEqual(1400);
  expect(poseAt(solo[0], script, 300, l).at).toBe("tools");   // walking over / grabbing
  const back = poseAt(solo[0], script, 1000, l);
  expect(back.at).toBe("desk");                                // running it at the desk
  expect(back.working).toBe(true);
  expect(back.bubble?.type).toBe("chip");
});

describe("a turn ingested as ONE root span (manual SDK, no children)", () => {
  // the real prod shape that showed "no agent said anything": the reply lives on the AGENT
  // root container's output, and there are no leaf beats at all
  const actors = [customer, actor("sup")];
  const layout = layoutOffice(actors);
  const script = toPlayEvents([
    ev(0, 0, "__customer__", "ask", "asks", { detail: "Cracked screen — replace it.", trace_id: "t1" }),
    ev(0, 650, "sup", "turn", "support-agent", { container: true, trace_id: "t1", detail: "RMA-2208 opened — label emailed." }),
  ], OFFICE_PACING).events;

  it("the agent speaks the envelope's answer instead of standing mute", () => {
    const during = poseAt(actors[1], script, 400, layout);
    // during the ask the sup is at the counter — and WORKING, their envelope is running
    expect(during.working).toBe(true);
    expect(during.bubble).toMatchObject({ type: "speech", text: "RMA-2208 opened — label emailed." });
    const after = poseAt(actors[1], script, 9000, layout);
    expect(after.bubble).toEqual({ type: "speech", text: "RMA-2208 opened — label emailed.", faded: true });
    expect(after.working).toBe(false);
  });
  it("the transcript lists the envelope answer, not 'no agent said anything'", () => {
    const digest = turnDigest(script, actors);
    expect(digest[0].words).toEqual([
      { actor: "sup", bubble: { type: "speech", text: "RMA-2208 opened — label emailed." } },
    ]);
  });
  it("the root still greets at the counter for a container-only turn", () => {
    expect(poseAt(actors[1], script, 100, layout).at).toBe("counter");
  });
  it("a silent llm mid-turn still never leaks the ending", () => {
    const s2 = toPlayEvents([
      ev(0, 2000, "sup", "turn", "graph", { container: true, detail: "The final answer.", trace_id: "t1" }),
      ev(100, 100, "sup", "llm", "step1", { detail: "" }),
      ev(400, 1500, "sup", "llm", "step2", { detail: "" }),
    ]).events;
    // step1 is in flight and silent — with leaf beats present, no envelope borrowing mid-turn
    expect(poseAt([actor("sup")][0], s2, 150, layoutOffice([actor("sup")])).bubble).toBeNull();
  });
});

describe("phones and alarms", () => {
  const actors = [actor("sup"), actor("faq", "sup", 1)];
  const layout = layoutOffice(actors);
  // delegation in flight play 0..600; faq's failing tool 400..700
  const script = toPlayEvents([
    ev(0, 600, "sup", "llm", "call", { delegate_to: "faq", say: "go" }),
    ev(700, 300, "faq", "tool", "boom", { station: "computer", status: "error" }),
  ]).events;

  it("the delegator's phone is off the hook, the callee's rings until they pick up work", () => {
    expect(phoneStateAt("sup", script, 300)).toBe("talking");
    expect(phoneStateAt("faq", script, 300)).toBe("ringing");
    expect(phoneStateAt("faq", script, 500)).toBe("idle"); // their own beat started
    expect(phoneStateAt("sup", script, 5000)).toBe("idle");
  });
  it("a failure keeps the office alarmed while hot, then cools off", () => {
    expect(alarmAt(script, 500)?.name).toBe("boom");
    expect(alarmAt(script, 700 + 1300)?.name).toBe("boom"); // still lingering
    expect(alarmAt(script, 5000)).toBeNull();
  });
  it("words type out while the beat is in flight", () => {
    const solo = [actor("a")];
    const l = layoutOffice(solo);
    const s = toPlayEvents([ev(0, 2000, "a", "llm", "chat", { detail: "Hello there, friend." })]).events;
    const p = poseAt(solo[0], s, 450, l).bubble?.progress;
    expect(p).toBeGreaterThan(0.3);
    expect(p).toBeLessThan(0.7);
    expect(poseAt(solo[0], s, 3000, l).bubble?.progress).toBeUndefined(); // finished = fully written
  });
});

describe("the customer's mood", () => {
  const actors = [customer, actor("sup")];
  const layout = layoutOffice(actors);
  it("pleased once the team's work landed, sour when a step failed", () => {
    const ok = toPlayEvents([
      ev(0, 0, "__customer__", "ask", "asks", { detail: "Hi", trace_id: "t1" }),
      ev(0, 400, "sup", "llm", "chat", { detail: "Done!", trace_id: "t1" }),
    ], OFFICE_PACING).events;
    expect(poseAt(customer, ok, 5000, layout).mood).toBe("happy");
    const bad = toPlayEvents([
      ev(0, 0, "__customer__", "ask", "asks", { detail: "Hi", trace_id: "t1" }),
      ev(0, 400, "sup", "tool", "boom", { trace_id: "t1", status: "error" }),
    ], OFFICE_PACING).events;
    expect(poseAt(customer, bad, 5000, layout).mood).toBe("grumpy");
  });
});

describe("the break room", () => {
  const actors = [actor("sup"), actor("faq", "sup", 1), actor("audit", "sup", 1)];
  const layout = layoutOffice(actors);
  // faq works 0..400; audit's only beat is far in the future; sup (root) is always on duty
  const script = toPlayEvents([
    ev(0, 400, "faq", "tool", "lookup", { station: "computer" }),
    ev(0, 400, "sup", "llm", "chat"),
    ev(9000, 400, "audit", "tool", "audit_log", { station: "computer" }),
  ], { maxGapMs: 100000 }).events; // keep the real gap so the idle window exists

  it("subs with nothing to do wander off; roots and the customer never do", () => {
    const plan = breakPlan([...actors, customer], script, 5000, layout);
    expect(plan.has("sup")).toBe(false);
    expect(plan.has("__customer__")).toBe(false);
    expect(plan.get("faq")).toBeDefined();   // done since 400 + settle
    expect(plan.get("audit")).toBeDefined(); // day hasn't reached their first task
  });
  it("two idle agents take the pong table, facing each other", () => {
    const plan = breakPlan(actors, script, 5000, layout);
    const kinds = [plan.get("faq")!.kind, plan.get("audit")!.kind].sort();
    expect(kinds).toEqual(["pong-a", "pong-b"]);
    expect(plan.get("faq")!.facing).not.toBe(plan.get("audit")!.facing);
  });
  it("a lone idle agent takes a coffee instead", () => {
    // at t=300 faq still works — only audit is idle
    const plan = breakPlan(actors, script, 300, layout);
    expect(plan.get("faq")).toBeUndefined();
    expect(plan.get("audit")).toMatchObject({ kind: "coffee" });
  });
  it("poseAt places a break-taker at the spot with a flavor line", () => {
    const plan = breakPlan(actors, script, 5000, layout);
    const p = poseAt(actors[1], script, 5000, layout, 0, plan.get("faq") ?? null);
    expect(p.at).toBe("break");
    expect(p.bubble?.type).toBe("thought");
    expect(p.working).toBe(false);
  });
  it("heads back to the desk before the next beat starts", () => {
    // audit's beat starts at play 9000·— within the walk lead nobody is on break anymore
    const start = script.find((e) => e.actor === "audit")!.pt;
    expect(breakPlan(actors, script, start - 200, layout).has("audit")).toBe(false);
  });
  it("without a break assignment poseAt stays exactly as before (desk)", () => {
    const p = poseAt(actors[1], script, 5000, layout);
    expect(p.at).toBe("desk");
  });
});

describe("bubble dwell", () => {
  // the prod shape: the model asks for a tool, the tool span starts ~1ms later. The newer beat
  // used to mask the older one instantly, so the decision flashed for a millisecond.
  const solo = [actor("sup")];
  const l = layoutOffice(solo);
  const script = toPlayEvents([
    ev(0, 1500, "sup", "llm", "chat", { detail: "→ get_balance", calls: ["get_balance"] }),
    ev(1, 3, "sup", "tool", "get_balance", { station: "computer", detail: "1299" }),
  ], OFFICE_PACING).events;

  it("gives the masked beat a readable window instead of 1ms", () => {
    expect(script[1].pt - script[0].pt).toBe(1);            // 1ms apart on the play clock
    expect(bubbleAt("sup", script, 0)?.name).toBe("chat");
    expect(bubbleAt("sup", script, 300)?.name).toBe("chat"); // would have been "get_balance"
    expect(bubbleAt("sup", script, 700)?.name).toBe("get_balance");
    expect(poseAt(solo[0], script, 300, l).bubble).toEqual({ type: "chip", icon: "call", text: "get_balance", faded: false });
  });

  it("caps how far a burst can drift the bubble behind the action", () => {
    const burst = toPlayEvents(
      Array.from({ length: 6 }, (_, i) => ev(i, 5, "sup", "tool", `t${i}`, { station: "computer" })),
      OFFICE_PACING,
    ).events;
    const windows = burst.map((e) => bubbleAt("sup", burst, e.pt + 1300 + 5)?.name);
    expect(windows[windows.length - 1]).toBe("t5");   // the last beat is reachable, not stuck
  });
});
