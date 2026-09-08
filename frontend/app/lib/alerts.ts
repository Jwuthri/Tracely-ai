import type { StepType } from "./ruleFlow";
import type { Monitor } from "./api";

/** Alert triggers, recipes, and the draft ⇄ API-body mapping — the parts of /settings/alerts worth
 *  testing without a DOM.
 *
 *  A trigger belongs to one of two families, and the difference is the whole mental model:
 *  - `event`     — the pipeline fires it the moment it happens (a gate failed, a turn failed, a new
 *                  failure mode appeared). No window, no averages.
 *  - `threshold` — the worker evaluates a rate/average over a sliding window every 5 minutes.
 *
 *  Keep the trigger ids in step with `backend/tracely/domain/monitoring/conditions.py`
 *  (`EVENT_TYPES` / `POLLED_TYPES`) — the API rejects anything else. */

export type TriggerId =
  | "gate_failed"
  | "trace_failed"
  | "cluster_new"
  | "fail_rate_over"
  | "score_below"
  | "trace_failure_rate";

export type FieldId = "contains" | "score_name" | "env" | "threshold" | "window" | "samples";

export type TriggerMeta = {
  label: string;
  family: "event" | "threshold";
  /** One line, shown under the trigger select and on the row. */
  blurb: string;
  fields: FieldId[];
  /** Threshold triggers only: is the number a percentage (rate) or a raw score? */
  unit?: "percent" | "score";
};

export const TRIGGERS: Record<TriggerId, TriggerMeta> = {
  gate_failed: {
    label: "CI gate failed",
    family: "event",
    blurb: "A gate run finished FAIL — or NO_COVERAGE / INCOMPLETE, a suite that could not run or be fully checked.",
    fields: ["env", "contains"],
  },
  trace_failed: {
    label: "Conversation failed",
    family: "event",
    blurb: "A production turn failed a non-advisory evaluator. Filter by evaluator or by the text of the failure.",
    fields: ["score_name", "contains"],
  },
  cluster_new: {
    label: "New failure mode",
    family: "event",
    blurb: "A failure signature nothing has produced before — a new cluster, not the 900th instance of a known one.",
    fields: ["contains"],
  },
  fail_rate_over: {
    label: "Evaluator FAIL rate over",
    family: "threshold",
    blurb: "One evaluator's FAIL rate across a window.",
    fields: ["score_name", "threshold", "window", "samples"],
    unit: "percent",
  },
  score_below: {
    label: "Average score below",
    family: "threshold",
    blurb: "One evaluator's average numeric score across a window.",
    fields: ["score_name", "threshold", "window", "samples"],
    unit: "score",
  },
  trace_failure_rate: {
    label: "Overall failure rate over",
    family: "threshold",
    blurb: "Share of traces failing any non-advisory evaluator across a window.",
    fields: ["threshold", "window", "samples"],
    unit: "percent",
  },
};

export type Draft = {
  name: string;
  description: string;
  target_agent: string;
  type: TriggerId;
  contains: string;
  score_name: string;
  env: string;
  /** Always 0..1 in the draft, exactly as the API stores it. The form renders percent triggers
   *  ×100 — converting in the input, not in the state, is what keeps 0.2 from becoming 0.002. */
  threshold: number;
  window_minutes: number;
  min_samples: number;
  min_interval_seconds: number;
  enabled: boolean;
};

const BASE: Draft = {
  name: "",
  description: "",
  target_agent: "",
  type: "gate_failed",
  contains: "",
  score_name: "",
  env: "",
  threshold: 0.2,
  window_minutes: 60,
  min_samples: 20,
  min_interval_seconds: 900,
  enabled: true,
};

/** A step in a recipe's starter flow. Ids are minted when the recipe is opened, so a recipe is
 *  data and never a half-saved rule. */
export type RecipeStep = { name: string; step_type: StepType; config: Record<string, unknown> };

const slackStep = (name: string, text: string): RecipeStep => ({
  name,
  step_type: "slack",
  config: { url: "", text_template: text },
});

/** Ready-made alerts, one per thing teams actually want to know about. The gallery is the answer
 *  to "what should I even alert on?" — every entry is a real Tracely signal, and every one opens as
 *  a working flow you finish by pasting one URL. */
export const RECIPES: { title: string; why: string; draft: Partial<Draft>; steps: RecipeStep[] }[] = [
  {
    title: "A PR's gate just failed",
    why: "The regression suite or a scenario broke on a pull request. Also catches the suite that could not run — the quietly-green case.",
    draft: { name: "CI gate failed", type: "gate_failed", env: "ci", min_interval_seconds: 0 },
    steps: [
      slackStep(
        "Post to Slack",
        "🚨 *{{ gate.status }}* on `{{ agent.slug }}` ({{ gate.env }})\n" +
          "{{ gate.failed }} failed / {{ gate.passed }} passed{% if gate.pr_number %} · PR #{{ gate.pr_number }}{% endif %}\n" +
          "<{{ gate.url }}|Open the gate run>",
      ),
    ],
  },
  {
    title: "A new failure mode in production",
    why: "A failure signature nobody has seen before. The one alert you cannot get from logs or a dashboard.",
    draft: { name: "New failure mode", type: "cluster_new", min_interval_seconds: 900 },
    steps: [
      slackStep(
        "Post to Slack",
        "🆕 New failure mode on `{{ agent.slug }}`\n*{{ cluster.label }}* ({{ cluster.taxonomy }})\n" +
          "<{{ cluster.url }}|Triage the cluster>",
      ),
    ],
  },
  {
    title: "A conversation leaked PII",
    why: "Your PII/policy judge failed a live turn. Swap the phrase for whatever your judge says when it catches the thing you fear.",
    draft: { name: "PII in a live conversation", type: "trace_failed", contains: "pii", min_interval_seconds: 300 },
    steps: [
      slackStep(
        "Post to Slack",
        "⚠️ *{{ failing_evaluators | join(', ') }}* on `{{ agent.slug }}`\n" +
          "{{ failure_reason }}\n> {{ trace.input }}\n<{{ trace.url }}|Open the turn>",
      ),
    ],
  },
  {
    title: "A specific evaluator started failing",
    why: "Scoped to one column — the tool-choice check, the grounding judge, the refund-policy rule.",
    draft: { name: "Evaluator failing live", type: "trace_failed", score_name: "", min_interval_seconds: 900 },
    steps: [
      {
        name: "Only the refund flow",
        step_type: "condition",
        config: { expression: "{{ 'refund' in failure_reason | lower }}" },
      },
      slackStep("Post to Slack", "*{{ alert.name }}*\n{{ failure_reason }}\n<{{ trace.url }}|Open the turn>"),
    ],
  },
  {
    title: "Quality is sliding after a deploy",
    why: "One evaluator's FAIL rate over the last hour crosses a line. The classic post-release watch.",
    draft: {
      name: "Quality regression",
      type: "fail_rate_over",
      threshold: 0.2,
      window_minutes: 60,
      min_samples: 20,
      min_interval_seconds: 1800,
    },
    steps: [
      slackStep(
        "Post to Slack",
        "📉 {{ alert.summary }}\nWatching `{{ metric.name }}` over {{ metric.window_minutes }} min.",
      ),
    ],
  },
  {
    title: "Goal completion is dropping",
    why: "Average conversation score below your bar — the agent still answers, it just stops finishing the job.",
    draft: {
      name: "Goal success below bar",
      type: "score_below",
      threshold: 0.6,
      window_minutes: 180,
      min_samples: 10,
      min_interval_seconds: 1800,
    },
    steps: [slackStep("Post to Slack", "📉 {{ alert.summary }}")],
  },
  {
    title: "Something broke everywhere",
    why: "Overall trace failure rate spiking — a bad model swap, an expired key, a tool returning 500s.",
    draft: {
      name: "Failure rate spike",
      type: "trace_failure_rate",
      threshold: 0.25,
      window_minutes: 30,
      min_samples: 25,
      min_interval_seconds: 900,
    },
    steps: [
      {
        name: "Page on-call",
        step_type: "webhook",
        config: {
          url: "",
          method: "POST",
          headers: [{ key: "Authorization", value: "Bearer " }],
          body_template: '{"summary": "{{ alert.summary }}", "url": "{{ alert.url }}"}',
        },
      },
    ],
  },
];

export function emptyDraft(patch: Partial<Draft> = {}): Draft {
  return { ...BASE, ...patch };
}

export function fromMonitor(m: Monitor): Draft {
  const c = m.condition ?? {};
  const type = (TRIGGERS[c.type as TriggerId] ? c.type : "gate_failed") as TriggerId;
  return {
    name: m.name,
    description: m.description ?? "",
    target_agent: m.target_agent ?? "",
    type,
    contains: c.contains ?? "",
    score_name: c.score_name ?? "",
    env: c.env ?? "",
    threshold: c.threshold ?? BASE.threshold,
    window_minutes: c.window_minutes ?? BASE.window_minutes,
    min_samples: c.min_samples ?? BASE.min_samples,
    min_interval_seconds: m.min_interval_seconds ?? BASE.min_interval_seconds,
    enabled: m.enabled,
  };
}

/** Draft → POST/PATCH body. Only the fields THIS trigger uses are sent: a leftover `threshold` on
 *  an event condition would read as a filter that isn't there, and a stale `contains` would
 *  silently narrow an alert the user thinks is wide open. */
export function toBody(d: Draft) {
  const meta = TRIGGERS[d.type];
  const condition: Record<string, unknown> = { type: d.type };
  if (meta.fields.includes("contains") && d.contains.trim()) condition.contains = d.contains.trim();
  if (meta.fields.includes("score_name") && d.score_name.trim()) condition.score_name = d.score_name.trim();
  if (meta.fields.includes("env") && d.env.trim()) condition.env = d.env.trim();
  if (meta.family === "threshold") {
    condition.threshold = d.threshold;
    condition.window_minutes = d.window_minutes;
    condition.min_samples = d.min_samples;
  }
  return {
    name: d.name.trim(),
    description: d.description.trim(),
    target_agent: d.target_agent.trim(),
    condition,
    enabled: d.enabled,
    min_interval_seconds: d.min_interval_seconds,
  };
}

/** Why Save is disabled, or null when the trigger half of the rule is complete. The flow half is
 *  validated by the canvas (a cycle, or no wired step). */
export function draftProblem(d: Draft): string | null {
  if (!d.name.trim()) return "Name the alert.";
  const meta = TRIGGERS[d.type];
  if (meta.fields.includes("score_name") && meta.family === "threshold" && !d.score_name.trim())
    return "Pick the evaluator to watch.";
  return null;
}

/** The trigger, in words, for the canvas's When node and the rule list: the label plus whichever
 *  filters are actually set. "" filters read as "everything of this type", which is the common
 *  case and should look deliberate rather than unconfigured. */
export function triggerSummary(d: {
  type: TriggerId;
  target_agent?: string;
  env?: string;
  score_name?: string;
  contains?: string;
  threshold?: number;
  window_minutes?: number;
}): string {
  const meta = TRIGGERS[d.type];
  const parts: string[] = [];
  if (d.env) parts.push(`env ${d.env}`);
  if (d.score_name) parts.push(d.score_name);
  if (d.contains) parts.push(`“${d.contains}”`);
  if (meta?.family === "threshold" && d.threshold !== undefined) {
    parts.push(
      meta.unit === "percent"
        ? `> ${Math.round(d.threshold * 100)}% / ${d.window_minutes ?? 60}min`
        : `< ${d.threshold} / ${d.window_minutes ?? 60}min`,
    );
  }
  parts.push(d.target_agent ? d.target_agent : "all agents");
  return parts.join(" · ");
}

/** "fires at most once every 15 min" — the interval, in words, for the row and the form. */
export function intervalLabel(seconds: number): string {
  if (!seconds) return "every time";
  if (seconds % 3600 === 0) return `at most 1×/${seconds / 3600}h`;
  return `at most 1×/${Math.round(seconds / 60)}min`;
}
