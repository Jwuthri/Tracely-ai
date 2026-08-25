"use client";

import clsx from "clsx";
import { useContext, useState } from "react";
import type { SessionSort, SortOrder } from "../../lib/api";
import type { EvaluatorCost, EvaluatorDef } from "../../lib/evaluators";
import { SelectBox } from "../SelectBox";
import { TypeChip } from "../ui";
import { fmtTokens } from "./format";
import { type Col, CTRL, HEAD_TH, LEVEL_BADGE, SORTABLE } from "./columns";
import { DotsIcon, Play } from "./icons";
import { CTRL_CELLS, EvalViewContext, SelectContext } from "./contexts";

/** How the table asks its owner to re-sort. Lives here, with the header that renders it, so the
 *  header module does not have to import a type back out of the root component. */
export type SortHandle = {
  sort: SessionSort;
  order: SortOrder;
  onSort: (key: SessionSort) => void;
  busy?: boolean;
};

// The header row and the menus that hang off it: column visibility, step-type filter, and the
// per-evaluator column controls.

// ── column-visibility menu ──────────────────────────────────────────────────────
function fmtCostCents(cents: number): string {
  // Show "<¢" for sub-cent totals so a real $0.003/30d isn't displayed as a fake $0.00.
  if (cents <= 0) return "<¢";
  if (cents < 100) return `${cents}¢`;
  return `$${(cents / 100).toFixed(cents < 10_000 ? 2 : 0)}`;
}

export function ColumnsMenu({ all, hidden, cost, onToggle, onClose }: { all: Col[]; hidden: Set<string>; cost: Record<string, EvaluatorCost>; onToggle: (k: string) => void; onClose: () => void }) {
  return (
    <>
      <div className="fixed inset-0 z-20" onClick={onClose} />
      <div className="absolute right-0 top-full z-30 mt-1 w-64 rounded-lg border border-line bg-ink-900 p-2 shadow-xl shadow-ink-950/50">
        <div className="px-2 py-1 text-[10px] uppercase tracking-wider text-fg-faint">Toggle columns · judge cost (30d)</div>
        <div className="max-h-72 overflow-auto">
          {all.map((col) => {
            const c = col.evaluator ? cost[col.evaluator.score_name] : undefined;
            return (
              <label key={col.key} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm text-fg hover:bg-ink-700">
                <input type="checkbox" checked={!hidden.has(col.key)} onChange={() => onToggle(col.key)} className="accent-signal" />
                <span className="truncate">{col.label}</span>
                {col.evaluator && <span className="rounded bg-signal/15 px-1 text-[9px] font-medium uppercase text-signal">eval</span>}
                {c && (
                  <span
                    className="rounded bg-ink-600/60 px-1 font-mono text-[9px] text-fg-muted"
                    title={`${c.runs} run(s) · ${c.total_tokens.toLocaleString()} tokens · ${fmtCostCents(c.cost_usd_cents)} over 30d${c.model ? ` · ${c.model}` : ""}`}
                  >
                    {fmtCostCents(c.cost_usd_cents)} · {fmtTokens(c.total_tokens)}
                  </span>
                )}
                <span className={clsx("ml-auto rounded px-1 text-[10px] font-medium", LEVEL_BADGE[col.group])}>{col.group}</span>
              </label>
            );
          })}
        </div>
      </div>
    </>
  );
}

// ── step-type filter menu ────────────────────────────────────────────────────────
// Hide noisy span types (e.g. the many CHAIN spans some frameworks emit) from the step rows.
// The filter has two layers: a WORKSPACE default everyone starts from, and this browser's own
// override once the user touches it — the footer moves the current set between the two.
export function TypesMenu({ types, hidden, source, onToggle, onReset, onUseDefault, onSaveDefault, onClose }: {
  types: string[]; hidden: Set<string>; source?: "local" | "workspace";
  onToggle: (t: string) => void; onReset: () => void;
  onUseDefault?: () => void; onSaveDefault?: () => void; onClose: () => void;
}) {
  return (
    <>
      <div className="fixed inset-0 z-20" onClick={onClose} />
      <div className="absolute right-0 top-full z-30 mt-1 w-56 rounded-lg border border-line bg-ink-900 p-2 shadow-xl shadow-ink-950/50">
        <div className="flex items-center justify-between px-2 py-1 text-[10px] uppercase tracking-wider text-fg-faint">
          <span>Filter step types</span>
          {hidden.size > 0 && (
            <button onClick={onReset} className="rounded px-1.5 py-0.5 text-[10px] normal-case tracking-normal text-signal hover:bg-ink-700 hover:text-signal" title="Show all types">
              Reset
            </button>
          )}
        </div>
        <div className="max-h-72 overflow-auto">
          {types.map((t) => (
            <label key={t} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm text-fg hover:bg-ink-700">
              <input type="checkbox" checked={!hidden.has(t)} onChange={() => onToggle(t)} className="accent-signal" />
              <TypeChip type={t} />
            </label>
          ))}
        </div>
        {(onSaveDefault || onUseDefault) && (
          <div className="mt-1 space-y-0.5 border-t border-line pt-1.5">
            {onSaveDefault && source === "local" && (
              <button onClick={onSaveDefault}
                title="Make the current filter the default for everyone in this workspace"
                className="block w-full rounded px-2 py-1 text-left text-[11px] text-fg-muted hover:bg-ink-700 hover:text-fg">
                Save as workspace default
              </button>
            )}
            {onUseDefault && source === "local" && (
              <button onClick={onUseDefault}
                title="Drop this browser's override and follow the workspace default"
                className="block w-full rounded px-2 py-1 text-left text-[11px] text-fg-muted hover:bg-ink-700 hover:text-fg">
                Use workspace default
              </button>
            )}
            {source === "workspace" && (
              <p className="px-2 py-1 text-[10px] text-fg-faint">following the workspace default</p>
            )}
          </div>
        )}
      </div>
    </>
  );
}

// ── header ──────────────────────────────────────────────────────────────────────
// Metric-column header controls: a Run button (whole column across the loaded rows) and a
// ⋯ menu with Edit / Delete.
function HeaderEvalControls({ evaluator }: { evaluator: EvaluatorDef }) {
  const view = useContext(EvalViewContext);
  const [menu, setMenu] = useState(false);
  const busy = view.busyCols.has(evaluator.score_name);
  return (
    <span className="ml-0.5 inline-flex items-center">
      {busy ? (
        <span className="inline-flex h-5 w-5 items-center justify-center" title="Evaluating…">
          <span className="h-3 w-3 animate-spin rounded-full border-2 border-line border-t-signal" />
        </span>
      ) : (
        <button
          onClick={() => view.runColumn(evaluator)}
          className="inline-flex h-5 w-5 items-center justify-center rounded text-ok transition-colors hover:bg-ink-600 hover:text-ok"
          title={`Run "${evaluator.name}" on all loaded rows`}
        >
          <Play className="h-3 w-3" />
        </button>
      )}
      <span className="relative">
        <button
          onClick={() => setMenu((o) => !o)}
          className="inline-flex h-5 w-5 items-center justify-center rounded text-fg-faint transition-colors hover:bg-ink-600 hover:text-fg"
          title="Column options"
        >
          <DotsIcon className="h-3 w-3" />
        </button>
        {menu && (
          <>
            <span className="fixed inset-0 z-20" onClick={() => setMenu(false)} />
            <span className="absolute right-0 top-full z-30 mt-1 block w-36 overflow-hidden rounded-lg border border-line bg-ink-900 py-1 shadow-xl shadow-ink-950/50">
              {!evaluator.enabled && (
                <span className="block px-3 py-1 text-[10px] font-medium uppercase tracking-wider text-warn/80">auto-run off</span>
              )}
              <button
                onClick={() => { setMenu(false); view.editColumn(evaluator); }}
                className="block w-full px-3 py-1.5 text-left text-xs font-normal normal-case tracking-normal text-fg hover:bg-ink-700"
              >
                Edit column
              </button>
              <button
                onClick={() => { setMenu(false); view.removeColumn(evaluator); }}
                className="block w-full px-3 py-1.5 text-left text-xs font-normal normal-case tracking-normal text-fail hover:bg-ink-700"
              >
                Delete column
              </button>
            </span>
          </>
        )}
      </span>
    </span>
  );
}

/** Sort affordance on a C-group header. Inactive columns show a muted glyph on hover only, so the
 *  header row stays quiet until you go looking for it. */
function SortGlyph({ active, order }: { active: boolean; order: SortOrder }) {
  return (
    <span
      aria-hidden
      className={clsx(
        "font-mono text-[9px] leading-none transition-opacity",
        active ? "text-signal opacity-100" : "text-fg-faint opacity-0 group-hover:opacity-60",
      )}
    >
      {active && order === "asc" ? "▲" : "▼"}
    </span>
  );
}

export function HeaderRow({ cols, sort }: { cols: Col[]; sort?: SortHandle }) {
  const sel = useContext(SelectContext);
  return (
    <tr className="border-b border-line bg-ink-700">
      {sel.enabled && (
        <th style={CTRL} className={HEAD_TH}>
          <SelectBox
            checked={sel.allSelected}
            indeterminate={sel.someSelected && !sel.allSelected}
            onChange={sel.toggleAll}
            label={sel.allSelected ? "Clear selection" : "Select all conversations"}
          />
        </th>
      )}
      {Array.from({ length: CTRL_CELLS }, (_, i) => (
        <th key={`ctrl-${i}`} style={CTRL} className={HEAD_TH} />
      ))}
      {cols.map((col, i) => {
        // Sorting reorders the whole list server-side, so it is only offered where the parent owns
        // the query (the /traces list). Embedded and shared views get plain headers.
        const sortKey = sort ? SORTABLE[col.key] : undefined;
        const active = sortKey !== undefined && sort!.sort === sortKey;
        const label = (
          <span
            className={clsx(col.evaluator && "max-w-[150px] truncate")}
            title={col.evaluator ? `${col.label} — ${col.evaluator.description || "evaluation column"}` : undefined}
          >
            {col.label}
          </span>
        );
        return (
          <th
            key={col.key}
            style={{ width: col.width, minWidth: 80 }}
            aria-sort={active ? (sort!.order === "asc" ? "ascending" : "descending") : undefined}
            className={clsx(
              HEAD_TH,
              (col.evaluator || (i > 0 && cols[i - 1].group !== col.group)) && "border-l border-line-bright/60",
              col.tint?.th,
            )}
          >
            <div className="flex items-center gap-1">
              {sortKey ? (
                <button
                  type="button"
                  onClick={() => sort!.onSort(sortKey)}
                  disabled={sort!.busy}
                  title={`Sort every conversation by ${col.label.toLowerCase()}${active ? (sort!.order === "desc" ? " (ascending)" : " (descending)") : ""}`}
                  className={clsx(
                    "group -mx-1 flex items-center gap-1 rounded px-1 py-0.5 uppercase tracking-wider transition-colors hover:bg-ink-600 disabled:opacity-50",
                    active ? "text-signal" : "text-fg-muted hover:text-fg",
                  )}
                >
                  {label}
                  <SortGlyph active={active} order={sort!.order} />
                </button>
              ) : (
                label
              )}
              <span className={clsx("rounded px-1.5 py-0.5 text-[10px] font-medium", LEVEL_BADGE[col.group])}>{col.group}</span>
              {col.evaluator && <HeaderEvalControls evaluator={col.evaluator} />}
            </div>
          </th>
        );
      })}
    </tr>
  );
}

