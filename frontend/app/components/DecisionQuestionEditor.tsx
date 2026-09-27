"use client";

import clsx from "clsx";

import type { DecisionOutputType, EvaluatorConfig } from "@/app/lib/evaluators";

// The Add Column body for a DECISION model (TypeSafe Jev). A decision model is a classifier: it
// reads the graded item as its `state` and answers a typed question with calibrated
// probabilities — no rubric, no free text. So instead of an evaluation prompt the column holds a
// question and the labels that define its answer space. Mirrors backend
// domain/evaluation/decision.py (validation limits included, so most mistakes never round-trip).

export type Label = { name: string; description: string; fail: boolean };

export type DecisionFields = {
  question: string;
  noulTrue: string;
  noulFalse: string;
  passWhen: "yes" | "no";
  labels: Label[];
};

const blankLabel = (): Label => ({ name: "", description: "", fail: false });

export const EMPTY_DECISION: DecisionFields = {
  question: "",
  noulTrue: "",
  noulFalse: "",
  passWhen: "yes",
  labels: [blankLabel(), blankLabel()],
};

export const MAX_LABELS: Record<Exclude<DecisionOutputType, "decision_binary">, number> = {
  decision_multiclass: 255,
  decision_multilabel: 50,
};

export const DECISION_TYPE_OPTIONS: { value: DecisionOutputType; label: string; hint: string }[] = [
  { value: "decision_binary", label: "Binary (yes / no)", hint: "Probability the answer is yes" },
  { value: "decision_multiclass", label: "Multi-class (pick one)", hint: "Exactly one label" },
  { value: "decision_multilabel", label: "Multi-label (pick any)", hint: "Every label that applies" },
];

export function decisionFromConfig(config: EvaluatorConfig): DecisionFields {
  const crit = config.criteria;
  const out: DecisionFields = { ...EMPTY_DECISION, question: config.question ?? "" };
  if (!crit || Array.isArray(crit)) return out;
  if (config.output_type === "decision_binary") {
    const c = crit as Record<string, string | null>;
    out.noulTrue = c.true ?? "";
    out.noulFalse = c.false ?? "";
    out.passWhen = config.pass_when === "no" ? "no" : "yes";
  } else {
    const fail = new Set(config.fail_options ?? []);
    out.labels = Object.entries(crit as Record<string, unknown>).map(([name, d]) => ({
      name,
      description: typeof d === "string" ? d : d == null ? "" : JSON.stringify(d),
      fail: fail.has(name),
    }));
  }
  return out;
}

/** Writes the decision keys onto `config` (and clears the ones this type doesn't use). */
export function applyDecision(config: EvaluatorConfig, type: DecisionOutputType, d: DecisionFields): void {
  config.question = d.question.trim();
  delete config.fail_options;
  delete config.pass_when;
  if (type === "decision_binary") {
    config.criteria = { true: d.noulTrue.trim(), false: d.noulFalse.trim() };
    config.pass_when = d.passWhen;
    return;
  }
  const labels = d.labels.filter((l) => l.name.trim());
  config.criteria = Object.fromEntries(labels.map((l) => [l.name.trim(), l.description.trim() || null]));
  config.fail_options = labels.filter((l) => l.fail).map((l) => l.name.trim());
}

export function clearDecision(config: EvaluatorConfig): void {
  delete config.question;
  delete config.criteria;
  delete config.fail_options;
  delete config.pass_when;
}

/** The first problem with the fields, or "" — checked before save so the user never waits on a 400. */
export function decisionProblem(type: DecisionOutputType, d: DecisionFields, threshold: string): string {
  if (!d.question.trim()) return "Write the question the model answers.";
  const t = parseFloat(threshold);
  if (type !== "decision_multiclass" && (Number.isNaN(t) || t <= 0 || t >= 1))
    return "The probability threshold must be between 0 and 1.";
  if (type === "decision_binary") {
    if (!d.noulTrue.trim() || !d.noulFalse.trim()) return "Describe what a Yes and a No mean.";
    return "";
  }
  const names = d.labels.map((l) => l.name.trim()).filter(Boolean);
  const max = MAX_LABELS[type];
  if (names.length < 2) return "Add at least two labels.";
  if (names.length > max) return `At most ${max} labels.`;
  if (new Set(names).size !== names.length) return "Label names must be unique.";
  const fails = d.labels.filter((l) => l.name.trim() && l.fail).length;
  if (type === "decision_multiclass" && fails === names.length)
    return "At least one label must not count as a failure.";
  return "";
}

const LABEL = "mb-1.5 block text-[10.5px] font-medium uppercase tracking-wider text-fg-faint";
const INPUT =
  "w-full rounded-lg border border-line bg-ink-900/60 px-3 py-2 text-[12.5px] text-fg placeholder:text-fg-faint/60 focus:border-signal/40 focus:outline-none";

const PLACEHOLDER: Record<DecisionOutputType, string> = {
  decision_binary: "Does the `Agent answer` fully resolve the `User request`?",
  decision_multiclass: "Which label best describes the `Agent answer`?",
  decision_multilabel: "What is the user asking about in the `User request`?",
};

export function DecisionQuestionEditor({
  type,
  value,
  onChange,
  threshold,
  onThreshold,
}: {
  type: DecisionOutputType;
  value: DecisionFields;
  onChange: (d: DecisionFields) => void;
  threshold: string;
  onThreshold: (t: string) => void;
}) {
  const set = (patch: Partial<DecisionFields>) => onChange({ ...value, ...patch });
  const setLabel = (i: number, patch: Partial<Label>) =>
    set({ labels: value.labels.map((l, j) => (j === i ? { ...l, ...patch } : l)) });

  const thresholdInput = (label: string) => (
    <div>
      <label className={LABEL}>{label}</label>
      <input value={threshold} onChange={(e) => onThreshold(e.target.value)} inputMode="decimal"
        aria-label={label} className={clsx(INPUT, "w-24 font-mono text-[12px]")} />
    </div>
  );

  return (
    <div className="space-y-3">
      <div>
        <label className={LABEL}>Question</label>
        <textarea
          value={value.question}
          onChange={(e) => set({ question: e.target.value })}
          rows={2}
          placeholder={PLACEHOLDER[type]}
          className={clsx(INPUT, "resize-y leading-relaxed")}
        />
        <p className="mt-1.5 text-[10.5px] text-fg-faint">
          One atomic question, read literally. The model reads the item itself (request, answer, steps) — not a rubric —
          so name the part you mean in backticks and state the exact condition.
        </p>
      </div>

      {type === "decision_binary" ? (
        <>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className={LABEL}>Yes means</label>
              <input value={value.noulTrue} onChange={(e) => set({ noulTrue: e.target.value })}
                placeholder="The answer does what was asked" className={INPUT} />
            </div>
            <div>
              <label className={LABEL}>No means</label>
              <input value={value.noulFalse} onChange={(e) => set({ noulFalse: e.target.value })}
                placeholder="The answer misses or misreads it" className={INPUT} />
            </div>
          </div>
          <div className="flex items-end gap-4">
            <div>
              <label className={LABEL}>Pass when</label>
              <div className="flex items-center gap-0.5 rounded-md border border-line bg-ink-900/60 p-0.5">
                {(["yes", "no"] as const).map((w) => (
                  <button key={w} type="button" onClick={() => set({ passWhen: w })}
                    className={clsx(
                      "rounded px-3 py-1 text-[11px] font-medium capitalize transition-colors",
                      value.passWhen === w ? "bg-ok/15 text-ok" : "text-fg-faint hover:text-fg-muted",
                    )}>
                    {w}
                  </button>
                ))}
              </div>
            </div>
            {thresholdInput("Yes when P(yes) ≥")}
          </div>
          <p className="text-[10.5px] text-fg-faint">
            Ask the question the way it reads naturally (&ldquo;Did the agent hallucinate?&rdquo;) and set <em>Pass when: No</em>{" "}
            — don&apos;t invert the criteria.
          </p>
        </>
      ) : (
        <div>
          <label className={LABEL}>
            Labels <span className="font-normal normal-case tracking-normal">({value.labels.length}/{MAX_LABELS[type]})</span>
          </label>
          <div className="space-y-1.5">
            {value.labels.map((l, i) => (
              <div key={i} className="flex items-center gap-2">
                <input value={l.name} placeholder="label" onChange={(e) => setLabel(i, { name: e.target.value })}
                  className={clsx(INPUT, "w-36 shrink-0 font-mono text-[12px]")} aria-label={`Label ${i + 1} name`} />
                <input value={l.description} placeholder="what this label means (optional)"
                  onChange={(e) => setLabel(i, { description: e.target.value })}
                  className={clsx(INPUT, "min-w-0 flex-1")} aria-label={`Label ${i + 1} description`} />
                <label className="flex shrink-0 cursor-pointer items-center gap-1 text-[10.5px] text-fg-faint"
                  title={type === "decision_multilabel" ? "The item FAILS if this label applies" : "Picking this label makes the item FAIL"}>
                  <input type="checkbox" checked={l.fail} className="accent-signal"
                    onChange={(e) => setLabel(i, { fail: e.target.checked })} />
                  fail
                </label>
                <button type="button" aria-label={`Remove label ${i + 1}`} disabled={value.labels.length <= 2}
                  onClick={() => set({ labels: value.labels.filter((_, j) => j !== i) })}
                  className="shrink-0 px-1 text-[13px] text-fg-faint hover:text-fg disabled:opacity-30">×</button>
              </div>
            ))}
          </div>
          <div className="mt-2 flex items-end justify-between gap-4">
            <button type="button" disabled={value.labels.length >= MAX_LABELS[type]}
              onClick={() => set({ labels: [...value.labels, blankLabel()] })}
              className="text-[11px] font-medium text-signal hover:underline disabled:opacity-40">
              + Add label
            </button>
            {type === "decision_multilabel" && thresholdInput("Applies when P ≥")}
          </div>
          <p className="mt-1.5 text-[10.5px] text-fg-faint">
            {type === "decision_multiclass"
              ? "Exactly one label is picked. "
              : "Each label is judged on its own (one yes/no per label, all in one call), so any number can apply. "}
            Tick <em>fail</em> on the labels that mean the item failed; with none ticked the column is informational.
          </p>
        </div>
      )}
    </div>
  );
}
