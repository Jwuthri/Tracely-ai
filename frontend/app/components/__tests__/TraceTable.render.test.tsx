import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// Mount-time effects fetch evaluator defs/costs + navigation — stub so the table renders offline.
// `push` is hoisted so a test can assert the row-click navigation did (or didn't) fire.
const { push } = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, prefetch: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));
vi.mock("@/app/lib/evaluators", async (orig) => ({
  ...(await orig<typeof import("@/app/lib/evaluators")>()),
  listEvaluators: vi.fn(() => Promise.resolve([])),
  getEvaluatorCost: vi.fn(() => Promise.resolve({})),
}));

import type { ConvNode, FullTurn, SpanOut } from "@/app/lib/api";
import { TraceTable } from "@/app/components/TraceTable";

function conv(over: Partial<ConvNode> = {}): ConvNode {
  return {
    thread: "thread-1",
    turns: 1,
    first_input: "Where is my order ORD-4471?",
    last_output: "It is out for delivery.",
    tokens: 120,
    cost: 0,
    first_ts: "2026-06-14T10:00:00Z",
    last_ts: "2026-06-14T10:00:05Z",
    last_trace_id: "trace-1",
    failing: 0,
    ...over,
  } as ConvNode;
}

describe("TraceTable (render safety net)", () => {
  it("renders column headers and a conversation row from its title", async () => {
    render(<TraceTable conversations={[conv()]} />);
    // header (C-group "Conversation" column label is unique among the defaults)
    expect(await screen.findByText("Conversation")).toBeInTheDocument();
    // the conversation row, titled from first_input via deriveTitle
    expect(screen.getByText(/Where is my order ORD-4471/)).toBeInTheDocument();
  });

  // A 1-turn conversation used to link straight to /traces/<id>, dropping Replay/Share/scenario.
  it("links a conversation row to its session even at one turn", async () => {
    render(<TraceTable conversations={[conv({ turns: 1 })]} />);
    const link = (await screen.findByText(/Where is my order ORD-4471/)).closest("a");
    expect(link).toHaveAttribute("href", "/sessions/thread-1");
  });

  it("shows an empty state when there are no conversations", async () => {
    render(<TraceTable conversations={[]} />);
    expect(await screen.findByText(/No conversations/i)).toBeInTheDocument();
  });

  // Multi-select delete: opt-in via onDeleted, DELETE /api/sessions with the picked threads.
  it("selects conversations and deletes them", async () => {
    const onDeleted = vi.fn();
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ threads: 1, traces: 2 }) }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("confirm", vi.fn(() => true));

    render(<TraceTable conversations={[conv(), conv({ thread: "thread-2", last_trace_id: "trace-2" })]} onDeleted={onDeleted} />);
    // header select-all + one box per conversation row
    const boxes = await screen.findAllByRole("checkbox");
    expect(boxes).toHaveLength(3);
    fireEvent.click(boxes[2]);
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    fireEvent.click(await screen.findByText("Delete"));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith(["thread-2"]));
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions", expect.objectContaining({ method: "DELETE", body: JSON.stringify({ threads: ["thread-2"] }) }));
    vi.unstubAllGlobals();
  });

  it("select-all picks every conversation, and picking one never navigates", async () => {
    push.mockClear();
    render(<TraceTable conversations={[conv(), conv({ thread: "thread-2", last_trace_id: "trace-2" })]} onDeleted={vi.fn()} />);
    const [selectAll, first] = await screen.findAllByRole("checkbox");

    fireEvent.click(first); // the row's checkbox must not trigger the row's navigate-on-click
    expect(push).not.toHaveBeenCalled();

    fireEvent.click(selectAll);
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    fireEvent.click(selectAll);
    expect(screen.queryByText(/selected/)).not.toBeInTheDocument();
  });

  it("has no checkboxes without onDeleted", async () => {
    render(<TraceTable conversations={[conv()]} />);
    await screen.findByText("Conversation");
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  // The conversation row's agent icon was a dead button; it must now open that thread's agent config.
  it("opens the conversation agents panel from the row's agent icon", async () => {
    const fetchMock = vi.fn((url: string) =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve(
          url.includes("/agents")
            ? { declared: [{ name: "Support Agent", description: "Handles support", tools: [] }], observed: [] }
            : {},
        ),
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<TraceTable conversations={[conv()]} />);
    fireEvent.click(await screen.findByTitle("View 1 agent"));
    expect(push).not.toHaveBeenCalled(); // it's a button click, not a row navigation

    expect(await screen.findByText("Conversation Agents")).toBeInTheDocument();
    expect(screen.getByText("thread-1")).toBeInTheDocument();
    expect(await screen.findByText("Support Agent")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Close"));
    expect(screen.queryByText("Conversation Agents")).not.toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});

// ── State Δ columns (M + S) ──────────────────────────────────────────────────
// Seeding `turnsData` puts the table in detail mode: spans are pre-loaded and every row is open,
// which is what the State Δ columns need to have anything to show.

function stateSpan(over: Partial<SpanOut> = {}): SpanOut {
  return {
    span_id: "sp-1", parent_span_id: "", name: "planner", type: "CHAIN", level: "DEFAULT",
    status_message: "", start_time: "2026-06-14T10:00:01Z", end_time: null, latency_ms: null,
    agent_id: "support", agent_run_id: "", turn_id: "", step_name: "planner", model_id: "",
    tokens: 0, cost: 0, metadata: { "tracely.state.plan": '["step-a"]' }, input: null, output: null,
    ...over,
  };
}

function turnWith(spans: SpanOut[]): FullTurn {
  return {
    trace_id: "trace-1", input: "hi", output: "done", tokens: 0, cost: 0, latency_ms: 10,
    ts: "2026-06-14T10:00:00Z", failing: 0, scores: [], verdict: null, spans,
  };
}

describe("TraceTable State Δ columns", () => {
  it("shows the written delta as a JSON object at step and message level", async () => {
    render(<TraceTable conversations={[conv({ turnsData: [turnWith([stateSpan()])] })]} />);
    // both the M and S columns carry the same header label
    expect(await screen.findAllByText("State Δ")).toHaveLength(2);
    // JsonPill preview: `plan:` key + its value — on the step row and the assistant message row
    expect(screen.getAllByText("plan:")).toHaveLength(2);
    expect(screen.getAllByText(/step-a/).length).toBeGreaterThanOrEqual(2);
  });

  it("merges a turn's steps into one net delta on the message row", async () => {
    const spans = [
      stateSpan({ span_id: "s1", metadata: { "tracely.state.plan": '["a"]' } }),
      stateSpan({
        span_id: "s2", step_name: "replan", start_time: "2026-06-14T10:00:02Z",
        metadata: { "tracely.state.plan": "[]", "tracely.state.retries": "1" },
      }),
    ];
    render(<TraceTable conversations={[conv({ turnsData: [turnWith(spans)] })]} />);
    await screen.findAllByText("State Δ");
    // the M row's pill previews the first key plus a "+1" for the second — the turn's net change
    expect(screen.getAllByText("+1").length).toBeGreaterThanOrEqual(1);
  });

  it("hides both columns entirely when nothing loaded carries state", async () => {
    const plain = stateSpan({ metadata: {}, output: "just an answer" });
    render(<TraceTable conversations={[conv({ turnsData: [turnWith([plain])] })]} />);
    expect(await screen.findByText("Conversation")).toBeInTheDocument();
    expect(screen.queryByText("State Δ")).not.toBeInTheDocument();
  });
});

// ── step-type filter ─────────────────────────────────────────────────────────
// The Types menu writes `hiddenTypes` into the shared prefs key. It used to be a no-op on the
// conversation Evals page: every span of a Tracely recording carries `tracely.internal.kind`, and
// those spans were exempt — so the same menu filtered the Timeline tab and did nothing to the
// Table tab, on the one page where every row is a recording.

function evalStep(over: Partial<SpanOut> = {}): SpanOut {
  return {
    ...stateSpan({ metadata: { "tracely.internal.kind": "eval" } }),
    span_id: "ev-1", step_name: "judge-group", type: "CHAIN",
    ...over,
  };
}

describe("TraceTable step-type filter", () => {
  const setHiddenTypes = (types: string[]) =>
    localStorage.setItem("tracely.traceTable.prefs", JSON.stringify({ hiddenTypes: types }));

  afterEach(() => localStorage.clear());

  it("hides a recording's steps too — the filter is not a no-op on the evals page", async () => {
    setHiddenTypes(["CHAIN"]);
    const spans = [
      evalStep(),
      evalStep({ span_id: "ev-2", step_name: "judge-call", type: "GENERATION" }),
    ];
    render(<TraceTable conversations={[conv({ turnsData: [turnWith(spans)] })]} />);

    expect(await screen.findByText("judge-call")).toBeInTheDocument();   // GENERATION kept
    expect(screen.queryByText("judge-group")).not.toBeInTheDocument();   // CHAIN filtered out
  });

  it("says so rather than rendering a blank turn when every type is hidden", async () => {
    setHiddenTypes(["CHAIN"]);
    render(<TraceTable conversations={[conv({ turnsData: [turnWith([evalStep()])] })]} />);
    expect(await screen.findByText(/All step types hidden/i)).toBeInTheDocument();
  });

  it("shows every step when nothing is hidden", async () => {
    render(<TraceTable conversations={[conv({ turnsData: [turnWith([evalStep()])] })]} />);
    expect(await screen.findByText("judge-group")).toBeInTheDocument();
  });
});

// The header, the body rows and the empty-state colSpan all count control cells independently.
// They drifted once — a 4th control cell was added to the body against a 3-cell header, which
// shifted every data column one to the left, so a verdict rendered under the wrong metric's name.
// Nothing about that fails loudly; it just displays the wrong thing. Hence a test.
describe("TraceTable column alignment", () => {
  const cells = (row: Element) => row.querySelectorAll(":scope > th, :scope > td").length;

  it("gives every body row exactly as many cells as the header", async () => {
    const { container } = render(<TraceTable conversations={[conv()]} />);
    await screen.findByText("Conversation");
    const [header, ...body] = Array.from(container.querySelectorAll("tr"));
    expect(body.length).toBeGreaterThan(0);
    body.forEach((row) => expect(cells(row)).toBe(cells(header)));
  });

  it("keeps them aligned with the select column on", async () => {
    const { container } = render(<TraceTable conversations={[conv()]} onDeleted={() => {}} />);
    await screen.findByText("Conversation");
    const [header, ...body] = Array.from(container.querySelectorAll("tr"));
    body.forEach((row) => expect(cells(row)).toBe(cells(header)));
  });

  it("spans the empty state across the full width", async () => {
    const { container } = render(<TraceTable conversations={[]} />);
    await screen.findByText(/No conversations/i);
    const [header, empty] = Array.from(container.querySelectorAll("tr"));
    expect(Number(empty.querySelector("td")!.getAttribute("colSpan"))).toBe(cells(header));
  });
});

// ── classification columns ───────────────────────────────────────────────────
// A json judge with no threshold emits no PASS/FAIL, so the cell's badge is the predicted label
// itself (`tracely.run.intent` → `checkout`) and the pill beside it carries the reason.

describe("TraceTable classification badge", () => {
  it("badges a verdict-less json score with its label, not with its raw JSON", async () => {
    const { listEvaluators } = await import("@/app/lib/evaluators");
    vi.mocked(listEvaluators).mockResolvedValueOnce([{
      id: "e1", name: "Conversation intent", description: "", kind: "llm_judge",
      score_name: "tracely.run.intent", level: "AGENT_RUN", enabled: true, config: {},
    }]);
    const turn: FullTurn = {
      ...turnWith([]),
      scores: [{
        name: "tracely.run.intent", evaluation_level: "AGENT_RUN", observation_id: null,
        value: null, verdict: "", comment: "user is paying", data_type: "TEXT",
        string_value: '{"intent":"checkout","reason":"user is paying"}',
      }],
    };
    render(<TraceTable conversations={[conv({ turnsData: [turn] })]} />);
    expect(await screen.findByText("checkout")).toBeInTheDocument();
    expect(screen.getByText("user is paying")).toBeInTheDocument();
  });
});

// ── rolling-summary fan-out ──────────────────────────────────────────────────
// The "Rolling summary" column is on by default, so every conversation row asks for its thread's
// summary on mount. Unbounded, that was one simultaneous request per row — enough to exhaust the
// backend's Postgres connections and 500 whatever else was rendering ("too many clients already").

describe("TraceTable rolling summary", () => {
  const rsumCalls = (m: ReturnType<typeof vi.fn>) =>
    m.mock.calls.filter((c) => String(c[0]).includes("rolling-summary"));

  afterEach(() => vi.unstubAllGlobals());

  it("loads one thread at a time instead of one request per row at once", async () => {
    let inFlight = 0;
    let peak = 0;
    // count only rolling-summary requests — unrelated mount-time fetches (e.g. the workspace
    // ui-prefs read) may legitimately overlap the first one
    const fetchMock = vi.fn((url: unknown) => {
      const isRsum = String(url).includes("rolling-summary");
      if (isRsum) peak = Math.max(peak, ++inFlight);
      return Promise.resolve({
        ok: true,
        json: () => { if (isRsum) inFlight--; return Promise.resolve({ conversation: [], traces: {}, spans: {} }); },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const many = Array.from({ length: 8 }, (_, i) => conv({ thread: `t-${i}`, last_trace_id: `tr-${i}` }));
    render(<TraceTable conversations={many} />);

    await waitFor(() => expect(rsumCalls(fetchMock)).toHaveLength(8));
    expect(peak).toBe(1);
  });

  // A failed load has to latch too, or `ensure`'s "already loaded?" guard never trips. Every
  // summary that DOES land re-renders the rows, which re-runs the failed row's effect and asks
  // again — so one blip amplifies into a permanent request storm rather than dying out.
  it("does not re-request a thread whose load failed", async () => {
    const fetchMock = vi.fn((url: string) =>
      url.includes("t-bad")
        ? Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve(null) })
        : Promise.resolve({ ok: true, json: () => Promise.resolve({ conversation: [], traces: {}, spans: {} }) }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<TraceTable conversations={[conv({ thread: "t-bad" }), conv({ thread: "t-ok", last_trace_id: "tr-2" })]} />);

    await waitFor(() => expect(rsumCalls(fetchMock)).toHaveLength(2));
    await new Promise((r) => setTimeout(r, 50)); // let the success's re-render settle
    expect(rsumCalls(fetchMock).filter((c) => String(c[0]).includes("t-bad"))).toHaveLength(1);
  });
});
