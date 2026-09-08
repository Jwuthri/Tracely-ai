import { describe, expect, it } from "vitest";
import { DEFAULT_VIEW, paramsFromView, queryFromView, viewFromParams } from "../TracesExplorer";

describe("traces view ⇄ URL", () => {
  it("round-trips every filter and omits defaults", () => {
    const v = viewFromParams({ range: "7d", agent: "a1", sort: "tokens", order: "asc", filter: "failing", q: "refund" });
    expect(v.preset).toBe("7d");
    expect(v.range.from).toBeTruthy();
    expect(paramsFromView(v)).toBe("range=7d&agent=a1&sort=tokens&order=asc&filter=failing&q=refund");
    expect(paramsFromView(DEFAULT_VIEW)).toBe("");
  });
  it("falls back on unknown values instead of erroring", () => {
    const v = viewFromParams({ range: "yesterday", sort: "colour", order: "sideways", filter: "weird" });
    expect(v.preset).toBe("all");
    expect(v.sortBy).toEqual({ sort: "recent", order: "desc" });
    expect(v.filter).toBe("all");
  });
  it("custom bounds survive and map to the server query", () => {
    const v = viewFromParams({ from: "2026-09-01T00:00:00.000Z", to: "2026-09-02T00:00:00.000Z" });
    expect(v.preset).toBe("custom");
    const q = queryFromView({ ...v, filter: "multi", q: "x" });
    expect(q).toMatchObject({ from: "2026-09-01T00:00:00.000Z", to: "2026-09-02T00:00:00.000Z", multi: true, q: "x" });
    expect(q.failing).toBeUndefined();
  });
});
