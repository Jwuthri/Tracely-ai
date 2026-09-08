import { describe, expect, it } from "vitest";
import { describeExpectations } from "./expectations";

describe("describeExpectations", () => {
  it("reads the default structural contract", () => {
    expect(describeExpectations({ required_tools: ["lookup"], match_mode: "superset", no_error: true })).toEqual([
      "Must call `lookup` (in any order, other tools allowed).",
      "The run must complete without any error.",
    ]);
  });
  it("names every editable expectation", () => {
    const lines = describeExpectations({
      required_tools: ["lookup"], forbidden_tools: ["delete_account"], match_mode: "strict",
      max_tool_calls: { lookup: 1 }, tool_args: [{ tool: "lookup", key: "order_id", equals: "42" }],
      allow_tool_errors: true, quality: { score_names: ["tracely.run.quality"] },
    });
    expect(lines).toContain("Must never call `delete_account`.");
    expect(lines).toContain("`lookup` at most 1×.");
    expect(lines).toContain('`lookup` must be called with order_id = "42".');
    expect(lines).toContain("A tool may fail, but the run itself must complete without error.");
    expect(lines[0]).toContain("exactly these tools, in this order");
    expect(lines.at(-1)).toContain("tracely.run.quality");
  });
  it("handles nothing", () => {
    expect(describeExpectations(null)[0]).toBe("No tool is required.");
  });
});
