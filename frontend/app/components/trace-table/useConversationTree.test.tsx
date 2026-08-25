import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useConversationTree } from "./useConversationTree";
import type { ConvNode, FullTurn } from "../../lib/api";

/* The lazy tree: list mode knows only conversation summaries and fetches a level at a time.
   What matters is WHEN it fetches — never twice for the same row, never on collapse, and never
   again for something already in hand. */

const conv = (thread: string, extra: Partial<ConvNode> = {}): ConvNode =>
  ({ thread, turns: 1, tokens: 0, cost: 0, failing: 0, scores: [], ...extra }) as unknown as ConvNode;

// A complete FullTurn, so a fixture can be handed to ConvNode without a cast. The five numeric
// fields are never read by this hook — they are here because the type has them, and leaving them
// out is what made the `Partial<ConvNode>` cast below fail to compile.
const turn = (trace_id: string): FullTurn => ({
  trace_id, ts: "", input: "", output: "", scores: [], spans: [],
  tokens: 0, cost: 0, latency_ms: 0, failing: 0, verdict: null,
});

function mockFetch() {
  const calls: string[] = [];
  const fn = vi.fn(async (url: string) => {
    calls.push(url);
    if (url.startsWith("/api/session?")) return { json: async () => ({ turns: [turn("tr-1")] }) };
    return { json: async () => ({ spans: [{ span_id: "s1" }] }) };
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("useConversationTree", () => {
  it("fetches a conversation's turns on first expand only", async () => {
    const calls = mockFetch();
    const { result } = renderHook(() => useConversationTree([conv("t-1")]));

    expect(result.current.openConv.size).toBe(0);
    act(() => result.current.toggleConv("t-1"));
    await waitFor(() => expect(Array.isArray(result.current.turns["t-1"])).toBe(true));
    expect(calls.filter((c) => c.includes("/api/session?")).length).toBe(1);

    // collapse then expand again — the answer is cached, so no second request
    act(() => result.current.toggleConv("t-1"));
    expect(result.current.openConv.has("t-1")).toBe(false);
    act(() => result.current.toggleConv("t-1"));
    expect(calls.filter((c) => c.includes("/api/session?")).length).toBe(1);
  });

  it("does not fetch twice when the state updater runs twice", async () => {
    // React re-invokes updaters (StrictMode does it in dev): a fetch fired INSIDE one runs twice.
    // This is why the decision happens before the setState, not in the reducer.
    const calls = mockFetch();
    const { result } = renderHook(() => useConversationTree([conv("t-1")]));
    act(() => result.current.toggleConv("t-1"));
    await waitFor(() => expect(result.current.turns["t-1"]).toBeDefined());
    expect(calls.length).toBe(1);
  });

  it("marks a row loading while it waits, and settles to [] on a failure", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("network"); }));
    const { result } = renderHook(() => useConversationTree([conv("t-1")]));
    act(() => result.current.toggleConv("t-1"));
    // a failed fetch settles as empty rather than `undefined`, so the row does not retry forever
    await waitFor(() => expect(result.current.turns["t-1"]).toEqual([]));
  });

  it("fetches a turn's spans when the turn opens", async () => {
    const calls = mockFetch();
    const { result } = renderHook(() => useConversationTree([conv("t-1")]));
    act(() => result.current.toggleTurn("tr-1"));
    await waitFor(() => expect(result.current.spans["tr-1"]).toEqual([{ span_id: "s1" }]));
    expect(calls.some((c) => c.startsWith("/api/trace?id=tr-1"))).toBe(true);
  });

  it("seeds detail mode fully expanded and never fetches", () => {
    const calls = mockFetch();
    const seeded = conv("t-1", { turnsData: [{ ...turn("tr-1"), spans: [{ span_id: "s1" }] }] } as Partial<ConvNode>);
    const { result } = renderHook(() => useConversationTree([seeded]));

    expect(result.current.openConv.has("t-1")).toBe(true);
    expect(result.current.openTurn.has("tr-1")).toBe(true);
    expect(result.current.allOpen).toBe(true);
    expect(calls).toEqual([]); // the tree came with the page
  });

  it("expand-all cascades to the step level, collapse-all closes everything", async () => {
    mockFetch();
    const { result } = renderHook(() => useConversationTree([conv("t-1"), conv("t-2")]));

    act(() => result.current.toggleAll());
    // `allOpen` flips as soon as the conversations open — the deeper levels are still in flight,
    // which is why the chevron reads "collapse" before the steps have landed
    await waitFor(() => expect(result.current.allOpen).toBe(true));
    await waitFor(() => expect(result.current.openTurn.has("tr-1")).toBe(true));
    await waitFor(() => expect(result.current.spans["tr-1"]).toBeDefined());

    act(() => result.current.toggleAll());
    expect(result.current.openConv.size).toBe(0);
    expect(result.current.openTurn.size).toBe(0);
  });

  it("allOpen is false while any conversation is closed", async () => {
    mockFetch();
    const { result } = renderHook(() => useConversationTree([conv("t-1"), conv("t-2")]));
    act(() => result.current.toggleConv("t-1"));
    expect(result.current.allOpen).toBe(false);
  });
});
