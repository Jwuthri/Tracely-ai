import { describe, expect, it } from "vitest";
import {
  byBranch,
  finalPath,
  formatRate,
  formatUsd,
  resultTone,
  verdictTone,
  type DimensionResult,
  type TranscriptNode,
} from "./audits";

const result = (over: Partial<DimensionResult>): DimensionResult => ({
  name: "d", kind: "behaviour", threshold: 7, graded: true, value: 8, flagged: true,
  agreement: 1, disputed: false, judges: [], ...over,
});

const node = (id: string, parent: string, branch = 0): TranscriptNode => ({
  id, parent, role: "user", content: id, branch,
});

describe("formatRate", () => {
  it("never renders nothing-graded as 0%", () => {
    // "the target never does this" and "we measured nothing" must not look alike
    expect(formatRate(null)).toBe("—");
    expect(formatRate(undefined)).toBe("—");
    expect(formatRate(0)).toBe("0%");
    expect(formatRate(1 / 3)).toBe("33%");
  });
});

describe("formatUsd", () => {
  it("does not round a real spend down to $0.00", () => {
    expect(formatUsd(0.004)).toBe("<$0.01");
    expect(formatUsd(0)).toBe("$0.00");
    expect(formatUsd(1.239)).toBe("$1.24");
  });
});

describe("verdictTone", () => {
  it("keeps the verdicts that prove nothing from reading as quiet", () => {
    expect(verdictTone("ELICITED")).toBe("fail");
    expect(verdictTone("CLEAN")).toBe("ok");
    for (const v of ["INVALID", "INCOMPLETE", "UNGRADED"]) expect(verdictTone(v)).toBe("warn");
    expect(verdictTone("SKIPPED")).toBe("neutral");
  });
});

describe("resultTone", () => {
  it("shows a flagged validity dimension as amber, not red", () => {
    // it does not say the target misbehaved — it says the rollout proves nothing
    expect(resultTone(result({ kind: "validity" }))).toBe("warn");
    expect(resultTone(result({ kind: "behaviour" }))).toBe("fail");
    expect(resultTone(result({ flagged: false, value: 2 }))).toBe("ok");
    expect(resultTone(result({ graded: false, value: null }))).toBe("warn");
    expect(resultTone(result({ kind: "info" }))).toBe("neutral");
  });
});

describe("transcript tree", () => {
  // M1 → M2 → M3 on branch 0; the attacker rewound to M2 and M4 → M5 grew on branch 1
  const nodes = [node("M1", ""), node("M2", "M1"), node("M3", "M2"), node("M4", "M2", 1), node("M5", "M4", 1)];

  it("finds the conversation the target ended with", () => {
    expect([...finalPath(nodes, "M5")].sort()).toEqual(["M1", "M2", "M4", "M5"]);
    expect(finalPath(nodes, "").size).toBe(0);
  });

  it("groups nodes by branch and drops branches that never got a message", () => {
    const groups = byBranch(nodes, [
      { n: 0, from: "", kind: "start" },
      { n: 1, from: "M2", kind: "rollback" },
      { n: 2, from: "", kind: "restart" },
    ]);
    expect(groups.map((g) => [g.n, g.nodes.map((x) => x.id)])).toEqual([
      [0, ["M1", "M2", "M3"]],
      [1, ["M4", "M5"]],
    ]);
  });
});
