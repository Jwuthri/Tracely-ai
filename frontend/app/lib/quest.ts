/* Pure logic for the onboarding quest — the gamified tour of every feature, plus the daily
   challenges that keep it alive after day one. Same philosophy as Activation.tsx: anything the
   product can count is DERIVED from real data (no stored progress to drift out of sync); only
   "go look at a page" steps live in localStorage as visit markers. Dates are UTC day-keys
   ("YYYY-MM-DD") to match the backend's trends buckets. */

export type QuestMilestone = { name: string; first_at: string | null; sample: boolean | null };

export type QuestStatus = {
  traces: number;
  evaluators: number;
  failures: number;
  clusters: number;
  cases: number;
  gates: number;
  llm_key: boolean;
  ingest_key: string | null;
  endpoint: string;
  /** today's (UTC) trends bucket + newest gate run — feeds the daily challenges */
  traces_today: number;
  failures_today: number;
  gate_today: boolean;
  /** newest conversation — lets Replay/Fleet steps deep-link instead of describing the path */
  thread_id: string | null;
  /** Durable activation milestones (W6). Sample rows never tick a step. */
  milestones?: QuestMilestone[];
};

export const EMPTY_STATUS: QuestStatus = {
  traces: 0,
  evaluators: 0,
  failures: 0,
  clusters: 0,
  cases: 0,
  gates: 0,
  llm_key: false,
  ingest_key: null,
  endpoint: "http://localhost:8000",
  traces_today: 0,
  failures_today: 0,
  gate_today: false,
  thread_id: null,
  milestones: [],
};

export type QuestDay = { date: string; visited: string[]; credited: string[] };

export type QuestLocal = {
  visited: string[];
  key_copied?: boolean;
  llm_skipped?: boolean;
  theme_touched?: boolean;
  opened?: boolean;
  celebrated?: boolean;
  dismissed?: boolean;
  /** today's challenge bookkeeping — rolls over when the date changes */
  daily?: QuestDay;
  /** lifetime points earned from daily challenges (quest xp is derived, this is not) */
  score?: number;
  streak?: { count: number; date: string };
};

export const EMPTY_LOCAL: QuestLocal = { visited: [] };

export type QuestStep = {
  id: string;
  group: string;
  title: string;
  detail: string;
  /** empty string = no link (the action lives in the chrome, e.g. the theme toggle) */
  href: string;
  cta: string;
  done: boolean;
  /** llm step only: no key, but the user said they don't have one. Counts as complete. */
  skipped?: boolean;
};

/** Which quest/daily marker (if any) a visit to `path` ticks. Order matters: a fleet/replay
 *  page is also a session page, so the more specific suffix wins. */
export function visitMarker(path: string): string | null {
  if (path.startsWith("/trends")) return "trends";
  if (path.startsWith("/settings/api-keys")) return "keys";
  if (path.startsWith("/calibration")) return "calibration";
  if (path.endsWith("/fleet")) return "fleet";
  if (path.endsWith("/replay")) return "replay";
  if (/^\/clusters\/.+/.test(path)) return "cluster";
  if (/^\/cases\/.+/.test(path)) return "case";
  if (/^\/(traces|sessions)\/.+/.test(path)) return "trace";
  return null;
}

export function deriveSteps(s: QuestStatus, l: QuestLocal): QuestStep[] {
  const seen = (m: string) => l.visited.includes(m);
  // A real (non-sample) durable milestone, or — for a workspace from before milestones — the count.
  const ms = s.milestones ?? [];
  const legacy = !ms.some((m) => m.first_at);
  const real = (name: string, fallback: boolean) =>
    ms.some((m) => m.name === name && !!m.first_at && !m.sample) || (legacy && fallback);
  const conv = (suffix: string) => (s.thread_id ? `/sessions/${s.thread_id}/${suffix}` : "/traces");
  return [
    {
      id: "key",
      group: "Set up",
      title: "Grab your API key",
      detail: "Every SDK call authenticates with this workspace's ingest key — copy it or manage keys in Settings.",
      href: "/settings/api-keys",
      cta: "Manage keys",
      done: !!l.key_copied || seen("keys"),
    },
    {
      id: "llm",
      group: "Set up",
      title: "Connect your OpenRouter key",
      detail: "Powers LLM-judge evaluators, failure clustering and meta-analysis. Structural checks run without it.",
      href: "/settings/llm",
      cta: "Add key",
      done: s.llm_key,
      skipped: !s.llm_key && !!l.llm_skipped,
    },
    {
      id: "trace",
      group: "Set up",
      title: "Send your first trace",
      detail: "pip install the SDK and point it at this workspace — auto-instrumentation does the rest.",
      href: "/dashboard",
      cta: "Get the snippet",
      done: real("first_trace_received", s.traces > 0),
    },
    {
      id: "open",
      group: "Explore",
      title: "Open a trace",
      detail: "Drill into a conversation: every turn, span, tool call and token, accounted for.",
      href: "/traces",
      cta: "Browse traces",
      done: seen("trace"),
    },
    {
      id: "eval",
      group: "Explore",
      title: "Add an evaluator column",
      detail: "Evaluators are the columns of the traces table — every new run gets graded automatically.",
      href: "/traces",
      cta: "+ Add column on Traces",
      done: s.evaluators > 0,
    },
    {
      id: "trends",
      group: "Explore",
      title: "Read your Trends",
      detail: "Daily volume, failure rate, gate pass rate — plus per-agent meta-analysis.",
      href: "/trends",
      cta: "Open Trends",
      done: seen("trends"),
    },
    {
      id: "replay",
      group: "Explore",
      title: "Watch a Replay",
      detail:
        "A conversation acted out on one scrubbable clock — a lane per agent, the step log following the playhead. On any conversation, it's the Replay tab.",
      href: conv("replay"),
      cta: s.thread_id ? "Replay your latest conversation" : "Open a conversation → Replay tab",
      done: seen("replay"),
    },
    {
      id: "fail",
      group: "Close the loop",
      title: "Catch a failure",
      detail: "Ticks itself the first time an evaluator fails a run; similar failures cluster into one issue.",
      href: "/clusters",
      cta: "See failure clusters",
      done: s.failures > 0 || s.clusters > 0,
    },
    {
      id: "case",
      group: "Close the loop",
      title: "Promote a regression case",
      detail: "A failure becomes a fail-to-pass test with the real tool calls recorded — it replays hermetically.",
      href: "/clusters",
      cta: "Promote from a cluster",
      done: real("source_failure_confirmed", s.cases > 0),
    },
    {
      id: "gate",
      group: "Close the loop",
      title: "Gate a pull request",
      detail: "Run the promoted cases against a PR's agent — exits non-zero, so the bug you fixed can't come back.",
      href: "/gates",
      cta: "See gate runs",
      done: real("ci_check_completed", s.gates > 0),
    },
  ];
}

export const stepComplete = (st: QuestStep) => st.done || !!st.skipped;

export const XP_PER_STEP = 10;

export function questRank(complete: number, total: number): string {
  const f = total ? complete / total : 0;
  if (f >= 1) return "Trace Master";
  if (f >= 0.7) return "Gate Keeper";
  if (f >= 0.4) return "Trace Detective";
  if (f > 0) return "Observer";
  return "Rookie";
}

// ── daily challenges ──────────────────────────────────────────────────────────

export type DailyChallenge = {
  id: string;
  title: string;
  detail: string;
  href: string;
  cta: string;
  points: number;
  done: boolean;
};

export const DAILIES_PER_DAY = 3;

/** Three challenges a day, rotated deterministically from the date — every browser agrees on
 *  today's set with nothing stored. Visit-shaped ones read TODAY's markers; the rest read the
 *  backend's today-bucket, so shipping traces or running CI in CI still counts. */
export function deriveDailies(s: QuestStatus, dayVisited: string[], dateKey: string): DailyChallenge[] {
  const seen = (m: string) => dayVisited.includes(m);
  const conv = (suffix: string) => (s.thread_id ? `/sessions/${s.thread_id}/${suffix}` : "/traces");
  const all: DailyChallenge[] = [
    {
      id: "view",
      title: "Open a trace",
      detail: "Read one real run end to end — the habit that catches drift early.",
      href: "/traces",
      cta: "Browse traces",
      points: 5,
      done: seen("trace"),
    },
    {
      id: "trends",
      title: "Check Trends",
      detail: "Thirty seconds on the failure-rate chart beats a surprise on Friday.",
      href: "/trends",
      cta: "Open Trends",
      points: 5,
      done: seen("trends"),
    },
    {
      id: "replay",
      title: "Watch a Replay",
      detail: "Scrub through a conversation on the clock — latency gaps jump out.",
      href: conv("replay"),
      cta: "Replay a conversation",
      points: 10,
      done: seen("replay"),
    },
    {
      id: "fleet",
      title: "Drop by the Fleet office",
      detail: "Watch your agents work their desks for one conversation.",
      href: conv("fleet"),
      cta: "Open the Fleet",
      points: 10,
      done: seen("fleet"),
    },
    {
      id: "cluster",
      title: "Triage a failure cluster",
      detail: "Open one cluster and decide: promote it, or ignore it. Untriaged piles rot.",
      href: "/clusters",
      cta: "Pick a cluster",
      points: 10,
      done: seen("cluster"),
    },
    {
      id: "case",
      title: "Review a regression case",
      detail: "Open a case and check its last verdict still means what you think.",
      href: "/cases",
      cta: "Open a case",
      points: 5,
      done: seen("case"),
    },
    {
      id: "calibration",
      title: "Check judge calibration",
      detail: "Label a few judge verdicts — agreement % is what makes evals trustworthy.",
      href: "/calibration",
      cta: "Open calibration",
      points: 10,
      done: seen("calibration"),
    },
    {
      id: "ship",
      title: "Ship traces today",
      detail: "At least one production trace landed today. Quiet pipes hide dead agents.",
      href: "/traces",
      cta: "See today's traces",
      points: 10,
      done: s.traces_today > 0,
    },
    {
      id: "gate",
      title: "Run a CI gate today",
      detail: "A gate run today — from CI or the Run button — keeps regressions expensive.",
      href: "/gates",
      cta: "Run a gate",
      points: 15,
      done: s.gate_today,
    },
    {
      id: "clean",
      title: "Clean day — zero failures",
      detail: "Traces shipped today and not one non-advisory FAIL. The good kind of quiet.",
      href: "/trends",
      cta: "Check the chart",
      points: 20,
      done: s.traces_today > 0 && s.failures_today === 0,
    },
  ];
  // adjacent-free spread: stride 3 over 10 items → three distinct picks for any start
  let h = 0;
  for (const c of dateKey) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const start = h % all.length;
  return Array.from({ length: DAILIES_PER_DAY }, (_, i) => all[(start + i * 3) % all.length]);
}

/** Credit newly-completed dailies into score + streak — returns the updated local, or null when
 *  nothing changed (so the caller can setState without looping). Also rolls the day over. */
export function settleDaily(l: QuestLocal, dailies: DailyChallenge[], dateKey: string): QuestLocal | null {
  const current = l.daily?.date === dateKey;
  const day: QuestDay = current ? l.daily! : { date: dateKey, visited: [], credited: [] };
  const fresh = dailies.filter((d) => d.done && !day.credited.includes(d.id));
  if (!fresh.length) return current ? null : { ...l, daily: day };

  let streak = l.streak ?? { count: 0, date: "" };
  if (streak.date !== dateKey) {
    const yesterday = new Date(Date.parse(`${dateKey}T00:00:00Z`) - 864e5).toISOString().slice(0, 10);
    streak = { count: streak.date === yesterday ? streak.count + 1 : 1, date: dateKey };
  }
  return {
    ...l,
    daily: { ...day, credited: [...day.credited, ...fresh.map((d) => d.id)] },
    score: (l.score ?? 0) + fresh.reduce((n, d) => n + d.points, 0),
    streak,
  };
}
