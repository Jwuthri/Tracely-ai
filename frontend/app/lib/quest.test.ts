import { describe, expect, it } from "vitest";
import {
  DAILIES_PER_DAY,
  EMPTY_LOCAL,
  EMPTY_STATUS,
  deriveDailies,
  deriveSteps,
  questRank,
  settleDaily,
  stepComplete,
  visitMarker,
  type QuestStatus,
} from "./quest";

const status = (over: Partial<QuestStatus> = {}): QuestStatus => ({ ...EMPTY_STATUS, ...over });

describe("visitMarker", () => {
  it("maps routes to the step they tick — most specific first", () => {
    expect(visitMarker("/trends")).toBe("trends");
    expect(visitMarker("/settings/api-keys")).toBe("keys");
    expect(visitMarker("/calibration")).toBe("calibration");
    expect(visitMarker("/sessions/th-1/fleet")).toBe("fleet");
    expect(visitMarker("/sessions/th-1/replay")).toBe("replay");
    expect(visitMarker("/sessions/th-1")).toBe("trace");
    expect(visitMarker("/traces/abc123")).toBe("trace");
    expect(visitMarker("/clusters/c-1")).toBe("cluster");
    expect(visitMarker("/cases/case-1")).toBe("case");
    // the LIST pages tick nothing — only opening a thing counts
    expect(visitMarker("/traces")).toBeNull();
    expect(visitMarker("/clusters")).toBeNull();
    expect(visitMarker("/cases")).toBeNull();
    expect(visitMarker("/dashboard")).toBeNull();
  });
});

describe("deriveSteps", () => {
  it("a fresh workspace has everything to do", () => {
    const steps = deriveSteps(status(), EMPTY_LOCAL);
    expect(steps).toHaveLength(10);
    expect(steps.filter(stepComplete)).toHaveLength(0);
  });

  it("data-derived steps read real counts (legacy workspace), visit steps read markers", () => {
    const steps = deriveSteps(
      status({ traces: 12, evaluators: 2, clusters: 1, llm_key: true }),
      { visited: ["trace", "trends", "replay"], theme_touched: true },
    );
    const done = Object.fromEntries(steps.map((s) => [s.id, stepComplete(s)]));
    expect(done).toEqual({
      key: false,
      llm: true,
      trace: true,
      open: true,
      eval: true,
      trends: true,
      replay: true,
      fail: true, // a cluster counts as a caught failure, same as Activation
      case: false,
      gate: false,
    });
  });

  it("outcome steps read durable milestones once any exist — and sample rows never tick", () => {
    const m = (name: string, sample: boolean) => ({ name, first_at: "2026-09-07T10:00:00Z", sample });
    const steps = deriveSteps(status({ traces: 500, cases: 4, gates: 2, milestones: [m("first_trace_received", false), m("source_failure_confirmed", true), m("ci_check_completed", true)] }), EMPTY_LOCAL);
    const byId = Object.fromEntries(steps.map((s) => [s.id, s.done]));
    expect(byId.trace).toBe(true);
    expect(byId.case).toBe(false); // 4 cases, all sample
    expect(byId.gate).toBe(false);
  });

  it("replay deep-links into the latest conversation when one exists", () => {
    const withThread = deriveSteps(status({ thread_id: "th-9" }), EMPTY_LOCAL);
    expect(withThread.find((s) => s.id === "replay")?.href).toBe("/sessions/th-9/replay");
    const without = deriveSteps(status(), EMPTY_LOCAL);
    expect(without.find((s) => s.id === "replay")?.href).toBe("/traces");
  });

  it("skipping the OpenRouter key completes the step without marking it done", () => {
    const [, llm] = deriveSteps(status(), { visited: [], llm_skipped: true });
    expect(llm.id).toBe("llm");
    expect(llm.done).toBe(false);
    expect(llm.skipped).toBe(true);
    expect(stepComplete(llm)).toBe(true);
    // ...but a real key wins over the skip flag
    const [, llm2] = deriveSteps(status({ llm_key: true }), { visited: [], llm_skipped: true });
    expect(llm2.done).toBe(true);
    expect(llm2.skipped).toBe(false);
  });
});

describe("deriveDailies", () => {
  it("picks the same three distinct challenges for the same date, different for another", () => {
    const a = deriveDailies(status(), [], "2026-08-18");
    const b = deriveDailies(status(), [], "2026-08-18");
    expect(a.map((d) => d.id)).toEqual(b.map((d) => d.id));
    expect(new Set(a.map((d) => d.id)).size).toBe(DAILIES_PER_DAY);
    const c = deriveDailies(status(), [], "2026-08-19");
    expect(c.map((d) => d.id)).not.toEqual(a.map((d) => d.id));
  });

  it("visit challenges read TODAY's markers; data challenges read the today-bucket", () => {
    const byId = (key: string, s: QuestStatus, visited: string[]) =>
      Object.fromEntries(deriveDailies(s, visited, key).map((d) => [d.id, d.done]));
    // find a date whose picks include "clean" to pin the interesting one
    let key = "2026-01-01";
    for (let i = 1; i <= 31; i++) {
      key = `2026-01-${String(i).padStart(2, "0")}`;
      if (deriveDailies(status(), [], key).some((d) => d.id === "clean")) break;
    }
    expect(byId(key, status({ traces_today: 5, failures_today: 0 }), []).clean).toBe(true);
    expect(byId(key, status({ traces_today: 5, failures_today: 2 }), []).clean).toBe(false);
    expect(byId(key, status({ traces_today: 0, failures_today: 0 }), []).clean).toBe(false); // no traces ≠ clean
  });
});

describe("settleDaily", () => {
  const day = "2026-08-18";
  const dailies = (doneIds: string[]) =>
    deriveDailies(status(), [], day).map((d) => ({ ...d, done: doneIds.includes(d.id) }));

  it("credits a completion once, then settles to null", () => {
    const picks = deriveDailies(status(), [], day);
    const first = picks[0];
    const l1 = settleDaily({ visited: [] }, dailies([first.id]), day)!;
    expect(l1.score).toBe(first.points);
    expect(l1.streak).toEqual({ count: 1, date: day });
    expect(l1.daily?.credited).toEqual([first.id]);
    // same completion again: nothing to bank
    expect(settleDaily(l1, dailies([first.id]), day)).toBeNull();
  });

  it("streak increments on consecutive days and resets after a gap", () => {
    const base = { visited: [], streak: { count: 3, date: "2026-08-17" } };
    expect(settleDaily(base, dailies([deriveDailies(status(), [], day)[0].id]), day)!.streak).toEqual({
      count: 4,
      date: day,
    });
    const gapped = { visited: [], streak: { count: 3, date: "2026-08-10" } };
    expect(settleDaily(gapped, dailies([deriveDailies(status(), [], day)[0].id]), day)!.streak).toEqual({
      count: 1,
      date: day,
    });
  });

  it("rolls a stale day over (old visits/credits cleared) even with nothing new to credit", () => {
    const stale = { visited: [], score: 25, daily: { date: "2026-08-17", visited: ["trace"], credited: ["view"] } };
    const rolled = settleDaily(stale, dailies([]), day)!;
    expect(rolled.daily).toEqual({ date: day, visited: [], credited: [] });
    expect(rolled.score).toBe(25); // banked points survive the rollover
    // and once current with nothing new, it settles
    expect(settleDaily(rolled, dailies([]), day)).toBeNull();
  });
});

describe("questRank", () => {
  it("climbs from Rookie to Trace Master", () => {
    expect(questRank(0, 10)).toBe("Rookie");
    expect(questRank(1, 10)).toBe("Observer");
    expect(questRank(5, 10)).toBe("Trace Detective");
    expect(questRank(8, 10)).toBe("Gate Keeper");
    expect(questRank(10, 10)).toBe("Trace Master");
  });
});
