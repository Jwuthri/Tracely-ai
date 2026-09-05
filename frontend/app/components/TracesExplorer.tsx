"use client";

import clsx from "clsx";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { AgentRow, ConvNode, SessionSort, SortOrder } from "../lib/api";
import { mergeMeta, metaText } from "../lib/meta";
import { AgentPicker } from "./AgentPicker";
import { DateRangePicker } from "./DateRangePicker";
import { TraceTable } from "./TraceTable";

type Filter = "all" | "failing" | "multi";
type Range = { from: string | null; to: string | null }; // ISO-8601 (UTC); null = unbounded
type SortBy = { sort: SessionSort; order: SortOrder };

const DEFAULT_SORT: SortBy = { sort: "recent", order: "desc" };

const PRESETS: { key: string; label: string; hours: number | null }[] = [
  { key: "all", label: "All time", hours: null },
  { key: "24h", label: "24h", hours: 24 },
  { key: "7d", label: "7d", hours: 24 * 7 },
  { key: "30d", label: "30d", hours: 24 * 30 },
];

// The /traces landing: time-range + status + text filters over conversation threads, rendered as the
// hierarchical conv → message → step table. The thread list is server-paginated ("Load more"); status
// and text filters refine the rows already loaded (see plan note). The time range and pagination hit
// the backend via /api/sessions so the browser never holds the whole table.
export function TracesExplorer({
  initial,
  pageSize,
  hasMore: initialHasMore,
  agents = [],
}: {
  initial: ConvNode[];
  pageSize: number;
  hasMore: boolean;
  agents?: AgentRow[]; // the project's registry agents, for the Agent select
}) {
  const [rows, setRows] = useState<ConvNode[]>(initial);
  const [hasMore, setHasMore] = useState(initialHasMore);
  const [loading, setLoading] = useState(false);
  const [range, setRange] = useState<Range>({ from: null, to: null });
  const [preset, setPreset] = useState<string>("all");
  // Server-side, like the range: a customer's conversations can sit past the loaded page, so
  // "only this agent" has to be a query, not a filter over the 50 rows on screen. "" = every agent.
  const [agentId, setAgentId] = useState("");
  // Sorting is server-side (see lib/api::SessionSort): the list is a page, so ordering only the
  // loaded rows would make "longest first" mean "longest of the 50 already on screen".
  const [sortBy, setSortBy] = useState<SortBy>(DEFAULT_SORT);

  const [filter, setFilter] = useState<Filter>("all");
  const [q, setQ] = useState("");
  // Tracely's own runs (an evaluation, a scenario) are never listed here — the backend excludes
  // them unless asked, and they are read from the conversation they graded ("Show evals" on the
  // conversation page). Listing them alongside real traces turned 12 conversations into 54 rows.

  // Re-seed from the server when the page re-renders (e.g. after switching workspace), resetting the
  // window back to the first page.
  useEffect(() => {
    setRows(initial);
    setHasMore(initialHasMore);
    setRange({ from: null, to: null });
    setPreset("all");
    setAgentId("");
    setFilter("all");
    setSortBy(DEFAULT_SORT);
  }, [initial, initialHasMore]);

  // `next`, `by` and `who` are passed in rather than read from state: every caller is reacting to
  // a change that hasn't been committed yet, so reading state here would page with the previous
  // window/order/agent.
  const load = useCallback(
    async (next: Range, by: SortBy, who: string, offset: number, replace: boolean, limit = pageSize) => {
      setLoading(true);
      try {
        const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
        if (next.from) qs.set("from", next.from);
        if (next.to) qs.set("to", next.to);
        if (who) qs.set("agent", who);
        if (by.sort !== DEFAULT_SORT.sort || by.order !== DEFAULT_SORT.order) {
          qs.set("sort", by.sort);
          qs.set("order", by.order);
        }
        const r = await fetch(`/api/sessions?${qs.toString()}`, { cache: "no-store" });
        const data: ConvNode[] = r.ok ? await r.json() : [];
        setRows((prev) => (replace ? data : [...prev, ...data]));
        setHasMore(data.length === limit);
      } finally {
        setLoading(false);
      }
    },
    [pageSize],
  );

  // After a delete: drop the rows immediately (instant feedback), then re-read the same-sized
  // window from the server so the freed slots refill with the threads that were below the fold.
  const onDeleted = useCallback(
    (threads: string[]) => {
      setRows((prev) => prev.filter((r) => !threads.includes(r.thread)));
      void load(range, sortBy, agentId, 0, true, Math.max(pageSize, rows.length - threads.length));
    },
    [load, range, sortBy, agentId, pageSize, rows.length],
  );

  // A column header: same column toggles direction, a new column starts descending — newest /
  // slowest / most-expensive first is what someone clicking "Duration" is looking for.
  const onSort = useCallback(
    (key: SessionSort) => {
      const next: SortBy =
        key === sortBy.sort
          ? { sort: key, order: sortBy.order === "desc" ? "asc" : "desc" }
          : { sort: key, order: "desc" };
      setSortBy(next);
      void load(range, next, agentId, 0, true);
    },
    [load, range, sortBy, agentId],
  );

  function applyPreset(p: (typeof PRESETS)[number]) {
    const next: Range = {
      from: p.hours == null ? null : new Date(Date.now() - p.hours * 3_600_000).toISOString(),
      to: null,
    };
    setPreset(p.key);
    setRange(next);
    void load(next, sortBy, agentId, 0, true);
  }

  // The picker hands back ISO bounds (or null/null when cleared). Clearing falls back to All time.
  function applyCustom(from: string | null, to: string | null) {
    if (!from && !to) {
      applyPreset(PRESETS[0]);
      return;
    }
    const next: Range = { from, to };
    setPreset("custom");
    setRange(next);
    void load(next, sortBy, agentId, 0, true);
  }

  function applyAgent(id: string) {
    setAgentId(id);
    void load(range, sortBy, id, 0, true);
  }

  // Every filter refines the rows already loaded — none of them needs the server.


  const counts = useMemo(
    () => ({
      all: rows.length,
      failing: rows.filter((t) => t.failing === 1).length,
      multi: rows.filter((t) => t.turns > 1).length,
    }),
    [rows],
  );

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return rows.filter((t) => {
      if (filter === "failing" && t.failing !== 1) return false;
      if (filter === "multi" && t.turns <= 1) return false;
      if (needle) {
        // user-set metadata comes aggregated from the backend (list); fall back to loaded spans.
        const meta =
          t.metadata && Object.keys(t.metadata).length
            ? t.metadata
            : mergeMeta((t.turnsData ?? []).flatMap((tt) => tt.spans));
        const hay = [t.first_input ?? "", t.last_output ?? "", t.model ?? "", t.agent ?? "", metaText(meta)].join(" ").toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    });
  }, [rows, filter, q]);

  const ranged = range.from != null || range.to != null || agentId !== "";

  return (
    <div className="space-y-3">
      {/* Time-range bar. The range-picker and agent popovers float above the table on their own
          z-50 — `.reveal` no longer leaves a transform behind, so this row is not a stacking
          context and does not trap them (see `fadeup` in tailwind.config.ts). */}
      <div className="reveal flex flex-wrap items-center gap-2" suppressHydrationWarning>
        <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-fg-faint">Range</span>
        <div className="flex items-center gap-1.5">
          {PRESETS.map((p) => (
            <button
              key={p.key}
              onClick={() => applyPreset(p)}
              disabled={loading}
              className={clsx(
                "rounded-lg border px-2.5 py-1.5 text-[12px] font-medium transition-colors disabled:opacity-50",
                preset === p.key
                  ? "border-signal/50 bg-signal/15 text-signal"
                  : "border-line bg-ink-800 text-fg-muted hover:text-fg",
              )}
            >
              {p.label}
            </button>
          ))}
        </div>
        <span className="h-5 w-px bg-line" aria-hidden />
        <DateRangePicker
          from={preset === "custom" ? range.from : null}
          to={preset === "custom" ? range.to : null}
          disabled={loading}
          onApply={applyCustom}
        />
        {agents.length > 0 && (
          <>
            <span className="h-5 w-px bg-line" aria-hidden />
            <label className="flex items-center gap-2">
              <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-fg-faint">Agent</span>
              <AgentPicker
                agents={agents}
                value={agentId}
                onChange={applyAgent}
                allLabel="All agents"
                ariaLabel="Filter by agent"
                id="traces-agent"
                disabled={loading}
                className="w-52 rounded-lg border border-line bg-ink-800 px-2 py-1.5 font-mono text-[12px] text-fg placeholder:text-fg-faint transition-colors focus:border-signal/40 focus:outline-none disabled:opacity-50"
              />
            </label>
          </>
        )}
      </div>

      {/* Status + text filters (refine the loaded rows) */}
      <div
        className="reveal flex flex-wrap items-center justify-between gap-3"
        style={{ animationDelay: "60ms" }}
        suppressHydrationWarning
      >
        <div className="flex items-center gap-1.5">
          {(["all", "failing", "multi"] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              disabled={loading}
              className={clsx(
                "rounded-lg border px-3 py-1.5 text-[12px] font-medium transition-colors disabled:opacity-50",
                filter !== f
                  ? "border-line bg-ink-800 text-fg-muted hover:text-fg"
                  : "border-signal/50 bg-signal/15 text-signal",
              )}
            >
              {f === "all" ? "All" : f === "failing" ? "Failing" : "Multi-turn"}
              <span className="ml-1.5 font-mono text-[10.5px] opacity-70">{counts[f]}</span>
            </button>
          ))}
        </div>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Filter by text, model, agent, metadata…"
          className="w-56 rounded-lg border border-line bg-ink-800 px-3 py-1.5 text-[12.5px] text-fg placeholder:text-fg-faint focus:border-signal/40 focus:outline-none"
          suppressHydrationWarning
        />
      </div>

      {rows.length === 0 ? (
        <div className="card px-4 py-14 text-center text-[13px] text-fg-faint">
          {loading ? (
            "Loading…"
          ) : ranged ? (
            "No traces match — widen the range or pick All time / All agents."
          ) : (
            <>
              No traces yet — send one with the SDK or point an OTLP exporter at{" "}
              <code className="text-fg-muted">/v1/traces</code>.
            </>
          )}
        </div>
      ) : (
        <div className="reveal space-y-3" style={{ animationDelay: "80ms" }}>
          {shown.length > 0 ? (
            <TraceTable
              conversations={shown}
              onDeleted={onDeleted}
              sort={{ ...sortBy, onSort, busy: loading }}
            />
          ) : (
            <div className="card px-4 py-10 text-center text-[13px] text-fg-faint">
              No loaded threads match this filter{hasMore ? " — try Load more." : "."}
            </div>
          )}

          <div className="flex items-center justify-center gap-3 pt-1">
            {hasMore ? (
              <button
                onClick={() => void load(range, sortBy, agentId, rows.length, false)}
                disabled={loading}
                className="rounded-lg border border-line bg-ink-800 px-4 py-2 text-[12.5px] font-medium text-fg-muted transition-colors hover:text-fg disabled:opacity-50"
              >
                {loading ? "Loading…" : "Load more"}
              </button>
            ) : null}
            <span className="font-mono text-[10.5px] text-fg-faint">
              {rows.length} loaded{hasMore ? "+" : ""}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
