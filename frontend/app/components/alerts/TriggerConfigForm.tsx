"use client";

import clsx from "clsx";
import type { AgentRow } from "@/app/lib/api";
import { TRIGGERS, intervalLabel, type Draft, type TriggerId } from "@/app/lib/alerts";
import { AgentPicker } from "../AgentPicker";
import { FIELD, LABEL, TONE } from "./tone";

/** The inspector for the **When** node: which event or threshold starts the flow, and how narrow.
 *  It lives on the canvas rather than in a settings box because the trigger is the first node of
 *  the graph — selecting it and configuring it is the same gesture as any other step. */

const INTERVALS = [0, 300, 900, 1800, 3600, 21600];

export function TriggerConfigForm({
  draft,
  agents,
  scoreNames,
  onChange,
}: {
  draft: Draft;
  agents: AgentRow[];
  scoreNames: string[];
  onChange: (patch: Partial<Draft>) => void;
}) {
  const meta = TRIGGERS[draft.type];
  const has = (f: string) => meta.fields.includes(f as never);
  return (
    <div className="flex min-h-0 flex-col overflow-hidden">
      <div className={clsx("flex shrink-0 items-center gap-2.5 border-b border-line px-4 py-2.5", TONE.signal.tint)}>
        <span className={clsx("grid h-8 w-8 place-items-center rounded-md text-[13px]", TONE.signal.chip)}>⚡</span>
        <div className="min-w-0">
          <div className={clsx("font-mono text-[9.5px] font-semibold uppercase tracking-[0.16em]", TONE.signal.fg)}>
            When — the trigger
          </div>
          <div className="truncate text-[13px] font-medium text-fg">{meta.label}</div>
        </div>
      </div>
      <div className="space-y-3.5 overflow-y-auto px-4 py-3.5">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <div className="space-y-1">
            <label htmlFor="tr-type" className={LABEL}>
              Trigger
            </label>
            <select
              id="tr-type"
              value={draft.type}
              onChange={(e) => onChange({ type: e.target.value as TriggerId })}
              className={FIELD}
            >
              {(Object.keys(TRIGGERS) as TriggerId[]).map((t) => (
                <option key={t} value={t}>
                  {TRIGGERS[t].label}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-1">
            <label htmlFor="tr-agent" className={LABEL}>
              Agent
            </label>
            <AgentPicker
              id="tr-agent"
              agents={agents}
              value={draft.target_agent}
              onChange={(v) => onChange({ target_agent: v })}
              by="slug"
              allLabel="all agents"
              className={FIELD}
            />
          </div>
          <div className="space-y-1">
            <label htmlFor="tr-interval" className={LABEL}>
              Rate limit
            </label>
            <select
              id="tr-interval"
              value={draft.min_interval_seconds}
              onChange={(e) => onChange({ min_interval_seconds: Number(e.target.value) })}
              className={FIELD}
            >
              {[...new Set([...INTERVALS, draft.min_interval_seconds])]
                .sort((a, b) => a - b)
                .map((v) => (
                  <option key={v} value={v}>
                    {intervalLabel(v)}
                  </option>
                ))}
            </select>
          </div>

          {has("score_name") ? (
            <div className="space-y-1">
              <label htmlFor="tr-score" className={LABEL}>
                Evaluator {meta.family === "event" ? "(optional)" : ""}
              </label>
              <input
                id="tr-score"
                list="tr-score-names"
                value={draft.score_name}
                onChange={(e) => onChange({ score_name: e.target.value })}
                placeholder={scoreNames[0] ?? "tracely.run.quality"}
                className={FIELD}
              />
              <datalist id="tr-score-names">
                {scoreNames.map((n) => (
                  <option key={n} value={n} />
                ))}
              </datalist>
            </div>
          ) : null}

          {has("contains") ? (
            <div className="space-y-1">
              <label htmlFor="tr-contains" className={LABEL}>
                Text contains (optional)
              </label>
              <input
                id="tr-contains"
                value={draft.contains}
                onChange={(e) => onChange({ contains: e.target.value })}
                placeholder="pii, refund, timeout…"
                className={FIELD}
              />
            </div>
          ) : null}

          {has("env") ? (
            <div className="space-y-1">
              <label htmlFor="tr-env" className={LABEL}>
                Env (optional)
              </label>
              <input
                id="tr-env"
                value={draft.env}
                onChange={(e) => onChange({ env: e.target.value })}
                placeholder="ci"
                className={FIELD}
              />
            </div>
          ) : null}

          {has("threshold") ? (
            <div className="space-y-1">
              <label htmlFor="tr-threshold" className={LABEL}>
                {meta.unit === "percent" ? "Over (%)" : "Below (score)"}
              </label>
              <input
                id="tr-threshold"
                type="number"
                min={0}
                step={meta.unit === "percent" ? 1 : 0.05}
                max={meta.unit === "percent" ? 100 : 1}
                value={meta.unit === "percent" ? Math.round(draft.threshold * 100) : draft.threshold}
                onChange={(e) => {
                  const n = Number(e.target.value);
                  onChange({ threshold: meta.unit === "percent" ? n / 100 : n });
                }}
                className={FIELD}
              />
            </div>
          ) : null}

          {has("window") ? (
            <div className="space-y-1">
              <label htmlFor="tr-window" className={LABEL}>
                Window (min)
              </label>
              <input
                id="tr-window"
                type="number"
                min={1}
                value={draft.window_minutes}
                onChange={(e) => onChange({ window_minutes: Number(e.target.value) })}
                className={FIELD}
              />
            </div>
          ) : null}

          {has("samples") ? (
            <div className="space-y-1">
              <label htmlFor="tr-samples" className={LABEL}>
                Min samples
              </label>
              <input
                id="tr-samples"
                type="number"
                min={1}
                value={draft.min_samples}
                onChange={(e) => onChange({ min_samples: Number(e.target.value) })}
                className={FIELD}
              />
              <p className="text-[11px] text-fg-faint">1 of 1 is not a 100% failure rate.</p>
            </div>
          ) : null}
        </div>

        <p className="text-[11.5px] leading-snug text-fg-muted">
          {meta.blurb}{" "}
          <span className="text-fg-faint">
            {meta.family === "event"
              ? "Fires the moment it happens, and the flow runs inline."
              : "Evaluated every 5 minutes by the worker."}
          </span>
        </p>
      </div>
    </div>
  );
}
