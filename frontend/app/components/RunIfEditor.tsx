"use client";

import clsx from "clsx";

import type { EvaluatorDef, RunIfCondition } from "@/app/lib/evaluators";
import { isDecisionOutput } from "@/app/lib/evaluators";

// "Run only when…" — the column runs on an item only when these conditions hold on its
// Depends On results (every one of them). The cheap-first pattern: a Jev classifier labels every
// turn, and an LLM explanation runs only on the turns it flagged. Mirrors backend
// domain/evaluation/conditions.py; a condition's column is added to Depends On on save.

type Field = RunIfCondition["field"];

/** The labels a column can produce, when they are known up front (decision labels, json enums). */
export function labelsOf(ev: EvaluatorDef | undefined): string[] {
  const cfg = ev?.config;
  if (!cfg) return [];
  if (isDecisionOutput(cfg.output_type) && cfg.output_type !== "decision_binary" && cfg.criteria && !Array.isArray(cfg.criteria)) {
    return Object.keys(cfg.criteria);
  }
  const props = (cfg.output_schema as { properties?: Record<string, { enum?: unknown[] }> } | undefined)?.properties;
  const first = props && Object.values(props).find((p) => Array.isArray(p?.enum));
  return first?.enum ? first.enum.map(String) : [];
}

/** The field a new condition on this column should start on — whatever the column is "about". */
function defaultField(ev: EvaluatorDef | undefined): Field {
  if (labelsOf(ev).length) return "label";
  return "verdict";
}

export function blankCondition(ev: EvaluatorDef): RunIfCondition {
  const field = defaultField(ev);
  return field === "label"
    ? { column: ev.score_name, field, op: "in", values: labelsOf(ev).slice(0, 1) }
    : { column: ev.score_name, field, op: "in", values: ["FAIL"] };
}

const SELECT =
  "rounded-md border border-line bg-ink-900/60 px-2 py-1 text-[11.5px] text-fg focus:border-signal/40 focus:outline-none";

export function RunIfEditor({
  candidates,
  value,
  onChange,
}: {
  candidates: EvaluatorDef[];
  value: RunIfCondition[];
  onChange: (next: RunIfCondition[]) => void;
}) {
  const byName = new Map(candidates.map((e) => [e.score_name, e]));
  const set = (i: number, patch: Partial<RunIfCondition>) =>
    onChange(value.map((c, j) => (j === i ? { ...c, ...patch } : c)));

  return (
    <div>
      <label className="mb-1 block text-[10.5px] font-medium uppercase tracking-wider text-fg-faint">
        Run only when{" "}
        <span className="rounded bg-ink-700 px-1 py-0.5 text-[9px] font-normal normal-case tracking-normal text-fg-faint">optional</span>
      </label>
      <p className="mb-2 text-[10.5px] text-fg-faint">
        Grade an item only when every condition holds on its Depends On results — e.g. explain with an LLM only the
        turns a Jev column labelled <span className="font-mono">refund</span>. Other items show &ldquo;Not run&rdquo;.
      </p>
      <div className="space-y-1.5">
        {value.map((c, i) => {
          const ev = byName.get(c.column);
          const labels = labelsOf(ev);
          return (
            <div key={i} className="flex flex-wrap items-center gap-1.5 rounded-lg border border-line bg-ink-900/40 p-2">
              <select className={SELECT} value={c.column} aria-label={`Condition ${i + 1} column`}
                onChange={(e) => {
                  const next = byName.get(e.target.value);
                  if (next) set(i, blankCondition(next));
                }}>
                {candidates.map((e) => <option key={e.id} value={e.score_name}>{e.name}</option>)}
              </select>
              <select className={SELECT} value={c.field} aria-label={`Condition ${i + 1} field`}
                onChange={(e) => {
                  const field = e.target.value as Field;
                  set(i, field === "value"
                    ? { field, op: "gte", value: 0.5, values: undefined }
                    : { field, op: "in", value: undefined, values: field === "verdict" ? ["FAIL"] : labels.slice(0, 1) });
                }}>
                <option value="label">label</option>
                <option value="verdict">verdict</option>
                <option value="value">value</option>
              </select>
              <select className={SELECT} value={c.op} aria-label={`Condition ${i + 1} operator`}
                onChange={(e) => set(i, { op: e.target.value as RunIfCondition["op"] })}>
                {c.field === "value" ? (
                  <><option value="gte">≥</option><option value="gt">&gt;</option><option value="lte">≤</option><option value="lt">&lt;</option></>
                ) : (
                  <><option value="in">is any of</option><option value="not_in">is none of</option></>
                )}
              </select>
              {c.field === "value" ? (
                <input className={clsx(SELECT, "w-20 font-mono")} inputMode="decimal" aria-label={`Condition ${i + 1} value`}
                  value={c.value ?? ""} onChange={(e) => set(i, { value: e.target.value === "" ? undefined : Number(e.target.value) })} />
              ) : (
                <span className="flex flex-wrap gap-1">
                  {(c.field === "verdict" ? ["PASS", "FAIL", "NONE"] : labels.length ? labels : c.values ?? []).map((opt) => {
                    const on = (c.values ?? []).includes(opt);
                    return (
                      <button key={opt} type="button"
                        onClick={() => set(i, { values: on ? (c.values ?? []).filter((v) => v !== opt) : [...(c.values ?? []), opt] })}
                        className={clsx("rounded border px-1.5 py-0.5 font-mono text-[10.5px] transition-colors",
                          on ? "border-signal/50 bg-signal/15 text-signal" : "border-line text-fg-faint hover:text-fg-muted")}>
                        {opt}
                      </button>
                    );
                  })}
                  {c.field === "label" && !labels.length && (
                    <input className={clsx(SELECT, "w-32 font-mono")} placeholder="label, label…" aria-label={`Condition ${i + 1} labels`}
                      value={(c.values ?? []).join(", ")}
                      onChange={(e) => set(i, { values: e.target.value.split(",").map((v) => v.trim()).filter(Boolean) })} />
                  )}
                </span>
              )}
              <button type="button" aria-label={`Remove condition ${i + 1}`} onClick={() => onChange(value.filter((_, j) => j !== i))}
                className="ml-auto px-1 text-[13px] text-fg-faint hover:text-fg">×</button>
            </div>
          );
        })}
      </div>
      {value.length < 5 && candidates.length > 0 && (
        <button type="button" onClick={() => onChange([...value, blankCondition(candidates[0])])}
          className="mt-2 text-[11px] font-medium text-signal hover:underline">
          + Add condition
        </button>
      )}
    </div>
  );
}

/** The first problem with the conditions, or "" — mirrors the backend's validation. */
export function runIfProblem(conds: RunIfCondition[]): string {
  for (const c of conds) {
    if (c.field === "value") {
      if (typeof c.value !== "number" || Number.isNaN(c.value)) return "Give each value condition a number.";
    } else if (!c.values?.length) {
      return `Pick at least one ${c.field} for each condition.`;
    }
  }
  return "";
}
