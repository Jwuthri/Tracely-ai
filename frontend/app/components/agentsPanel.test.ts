import { describe, expect, it } from "vitest";
import { notInCatalog, observedDetail, type AgentsData, type MergedAgent, type MergedTool } from "./agentsPanel";

const tool = (name: string, source: MergedTool["source"]): MergedTool => ({
  name, description: "", source, calls: 0, parameters: {},
});
const agent = (name: string, ids: string[], tools: MergedTool[] = []): MergedAgent => ({
  name, description: "", agent_ids: ids, tools,
});

describe("notInCatalog", () => {
  it("counts the tools the traces show but the agent definition left out", () => {
    const data: AgentsData = {
      agents: [agent("sup", ["a1"], [tool("route", "offered")]), agent("support", ["a1"], [tool("get_order", "declared"), tool("refund", "called")])],
      declared: [{ name: "support", description: "", tools: [{ name: "get_order" }] }],
      observed: [],
    };
    expect(notInCatalog(data)).toBe(2);
  });

  it("is 0 with no agent definition — nothing was left out of a catalog nobody sent", () => {
    expect(notInCatalog({ agents: [agent("x", ["a1"], [tool("t", "offered")])], declared: [], observed: [] })).toBe(0);
  });
});

describe("observedDetail", () => {
  const observed = [{ agent_id: "a1", system_prompt: "You route." }];

  it("hands an agent the prompt recovered from its own agent id", () => {
    const a = agent("router", ["a1"]);
    expect(observedDetail(a, { agents: [a], declared: [], observed })?.system_prompt).toBe("You route.");
  });

  it("withholds it when sub-agents share the id — the prompt would be the supervisor's", () => {
    const a = agent("lg-support", ["a1"]);
    const b = agent("lg-billing", ["a1"]);
    expect(observedDetail(a, { agents: [a, b], declared: [], observed })).toBeUndefined();
  });
});
