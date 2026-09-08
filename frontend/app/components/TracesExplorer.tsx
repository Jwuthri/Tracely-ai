"use client";

import clsx from "clsx";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentRow, ConvNode, SessionSort, SessionsQuery, SortOrder } from "../lib/api";
import { AgentPicker } from "./AgentPicker";
import { DateRangePicker } from "./DateRangePicker";
import { TraceTable } from "./TraceTable";

type Filter = "all" | "failing" | "multi";
type Range = { from: string | null; to: string | null }; // ISO-8601 (UTC); null = unbounded
type SortBy = { sort: SessionSort; order: SortOrder };

const DEFAULT_SORT: SortBy = { sort: "recent", order: "desc" };
const SORTS: SessionSort[] = ["recent", "started", "duration", "tokens"];

const PRESETS: { key: string; label: string; hours: number | null }[] = [
  { key: "all", label: "All time", hours: null },
  { key: "24h", label: "24h", hours: 24 },
  { key: "7d", label: "7d", hours: 24 * 7 },
  { key: "30d", label: "30d", hours: 24 * 30 },
];

/** The whole view, as the URL carries it: `?range=7d|from&to|agent=&sort=&order=&filter=&q=`. */
export type View = {
  preset: string;
  range: Range;
  agentId: string;
  sortBy: SortBy;
  filter: Filter;
  q: string;
};

export const DEFAULT_VIEW: View = {
  preset: "all", range: { from: null, to: null }, agentId: "", sortBy: DEFAULT_SORT, filter: "all", q: "",
};

/** URL → view. Unknown values fall back to the default rather than erroring — a stale shared link
 *  is a view preference, not a request to 400. */
export function viewFromParams(p: Record<string, string | undefined>): View {
  const preset = PRESETS.some((x) => x.key === p.range) ? p.range! : p.from || p.to ? "custom" : "all";
  const hours = PRESETS.find((x) => x.key === preset)?.hours ?? null;
  const range: Range =
    preset === "custom"
      ? { from: p.from || null, to: p.to || null }
      : { from: hours == null ? null : new Date(Date.now() - hours * 3_600_000).toISOString(), to: null };
  const sort = SORTS.includes(p.sort as SessionSort) ? (p.sort as SessionSort) : DEFAULT_SORT.sort;
  const order = p.order === "asc" || p.order === "desc" ? p.order : DEFAULT_SORT.order;
  const filter: Filter = p.filter === "failing" || p.filter === "multi" ? p.filter : "all";
  return { preset, range, agentId: p.agent ?? "", sortBy: { sort, order }, filter, q: (p.q ?? "").slice(0, 200) };
}

/** View → the server query it means (shared by the page's first render and the client). */
export function queryFromView(v: View): SessionsQuery {
  return {
    from: v.range.from, to: v.range.to, agent: v.agentId || undefined,
    sort: v.sortBy.sort, order: v.sortBy.order,
    failing: v.filter === "failing" || undefined, multi: v.filter === "multi" || undefined, q: v.q || undefined,
  };
}

export function queryFromParams(p: Record<string, string | undefined>): SessionsQuery {
  return queryFromView(viewFromParams(p));
}

/** View → URL search string, defaults omitted so the plain /traces stays plain. */
export function paramsFromView(v: View): string {
  const qs = new URLSearchParams();
  if (v.preset === "custom") {
    if (v.range.from) qs.set("from", v.range.from);
    if (v.range.to) qs.set("to", v.range.to);
  } else if (v.preset !== "all") qs.set("range", v.preset);
  if (v.agentId) qs.set("agent", v.agentId);
  if (v.sortBy.sort !== DEFAULT_SORT.sort || v.sortBy.order !== DEFAULT_SORT.order) {
    qs.set("sort", v.sortBy.sort);
    qs.set("order", v.sortBy.order);
  }
  if (v.filter !== "all") qs.set("filter", v.filter);
  if (v.q) qs.set("q", v.q);
  return qs.toString();
}

// The /traces landing: every filter — time range, agent, sort, status, multi-turn, text — is a
// server query over the whole thread set, never a pass over the rows on screen, so a match past
// the loaded page is found and the count, the empty state and the pages describe one result set.
// The view lives in the URL (shareable); responses are sequenced so a slow older query can never
// overwrite a newer one.
export function TracesExplorer({
  initial,
  initialTotal,
  initialView = DEFAULT_VIEW,
  pageSize,
  hasMore: initialHasMore,
  agents = [],
}: {
  initial: ConvNode[];
  initialTotal?: number;
  initialView?: View;
  pageSize: number;
  hasMore: boolean;
  agents?: AgentRow[]; // the project's registry agents, for the Agent select
}) {
  const router = useRouter();
  const search = useSearchParams();
  const [view, setView] = useState<View>(initialView);
  const [rows, setRows] = useState<ConvNode[]>(initial);
  const [total, setTotal] = useState<number | null>(initialTotal ?? null);
  const [hasMore, setHasMore] = useState(initialHasMore);
  const [loading, setLoading] = useState(false);
  const [qText, setQText] = useState(initialView.q);
  const seq = useRef(0); // the newest request wins; anything older is dropped on arrival

  // Re-seed from the server when the page re-renders (workspace switch, back navigation).
  useEffect(() => {
    setRows(initial);
    setHasMore(initialHasMore);
    setTotal(initialTotal ?? null);
    setView(initialView);
    setQText(initialView.q);
  }, [initial, initialHasMore, initialTotal, initialView]);

  const fetchPage = useCallback(
    async (v: View, offset: number, replace: boolean, limit = pageSize) => {
      const mine = ++seq.current;
      setLoading(true);
      try {
        const q = queryFromView(v);
        const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
        if (q.from) qs.set("from", q.from);
        if (q.to) qs.set("to", q.to);
        if (q.agent) qs.set("agent", q.agent);
        if (q.sort !== DEFAULT_SORT.sort || q.order !== DEFAULT_SORT.order) {
          qs.set("sort", q.sort!);
          qs.set("order", q.order!);
        }
        if (q.failing) qs.set("failing", "true");
        if (q.multi) qs.set("multi", "true");
        if (q.q) qs.set("q", q.q);
        const countQs = new URLSearchParams(qs);
        countQs.delete("limit");
        countQs.delete("offset");
        const [r, c] = await Promise.all([
          fetch(`/api/sessions?${qs.toString()}`, { cache: "no-store" }),
          replace ? fetch(`/api/sessions/count?${countQs.toString()}`, { cache: "no-store" }) : null,
        ]);
        if (mine !== seq.current) return; // a newer filter change already superseded this
        const data: ConvNode[] = r.ok ? await r.json() : [];
        setRows((prev) => (replace ? data : [...prev, ...data]));
        setHasMore(data.length === limit);
        if (c) {
          const t = c.ok ? ((await c.json()) as { total?: number }).total : null;
          if (mine === seq.current) setTotal(typeof t === "number" ? t : null);
        }
      } finally {
        if (mine === seq.current) setLoading(false);
      }
    },
    [pageSize],
  );

  // One entry point for every view change: state, URL (replace — filters are not history), query.
  const apply = useCallback(
    (next: View) => {
      setView(next);
      const qs = paramsFromView(next);
      router.replace(qs ? `?${qs}` : "?", { scroll: false });
      void fetchPage(next, 0, true);
    },
    [router, fetchPage],
  );

  // Browser back/forward changes the URL underneath us: follow it.
  useEffect(() => {
    const fromUrl = viewFromParams(Object.fromEntries(search.entries()));
    if (paramsFromView(fromUrl) !== paramsFromView(view)) {
      setView(fromUrl);
      setQText(fromUrl.q);
      void fetchPage(fromUrl, 0, true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  // Text filter: debounced, then a server query like every other filter.
  useEffect(() => {
    if (qText === view.q) return;
    const t = setTimeout(() => apply({ ...view, q: qText.trim().slice(0, 200) }), 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qText]);

  // After a delete: drop the rows immediately, then re-read the same-sized window so the freed
  // slots refill with the threads that were below the fold.
  const onDeleted = useCallback(
    (threads: string[]) => {
      setRows((prev) => prev.filter((r) => !threads.includes(r.thread)));
      void fetchPage(view, 0, true, Math.max(pageSize, rows.length - threads.length));
    },
    [fetchPage, view, pageSize, rows.length],
  );

  const onSort = useCallback(
    (key: SessionSort) => {
      const next: SortBy =
        key === view.sortBy.sort
          ? { sort: key, order: view.sortBy.order === "desc" ? "asc" : "desc" }
          : { sort: key, order: "desc" };
      apply({ ...view, sortBy: next });
    },
    [apply, view],
  );

  function applyPreset(p: (typeof PRESETS)[number]) {
    apply({
      ...view,
      preset: p.key,
      range: { from: p.hours == null ? null : new Date(Date.now() - p.hours * 3_600_000).toISOString(), to: null },
    });
  }

  function applyCustom(from: string | null, to: string | null) {
    if (!from && !to) return applyPreset(PRESETS[0]);
    apply({ ...view, preset: "custom", range: { from, to } });
  }

  const filtered = view.range.from != null || view.range.to != null || view.agentId !== "" || view.filter !== "all" || view.q !== "";
  const shareUrl = paramsFromView(view);

  return (
    <div className="space-y-3">
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
                view.preset === p.key ? "border-signal/50 bg-signal/15 text-signal" : "border-line bg-ink-800 text-fg-muted hover:text-fg",
              )}
            >
              {p.label}
            </button>
          ))}
        </div>
        <span className="h-5 w-px bg-line" aria-hidden />
        <DateRangePicker
          from={view.preset === "custom" ? view.range.from : null}
          to={view.preset === "custom" ? view.range.to : null}
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
                value={view.agentId}
                onChange={(id) => apply({ ...view, agentId: id })}
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

      {/* Status + text filters — server-side over the whole set, like the range. */}
      <div className="reveal flex flex-wrap items-center justify-between gap-3" style={{ animationDelay: "60ms" }} suppressHydrationWarning>
        <div className="flex items-center gap-1.5" role="group" aria-label="Status filter">
          {(["all", "failing", "multi"] as const).map((f) => (
            <button
              key={f}
              onClick={() => apply({ ...view, filter: f })}
              disabled={loading}
              aria-pressed={view.filter === f}
              className={clsx(
                "rounded-lg border px-3 py-1.5 text-[12px] font-medium transition-colors disabled:opacity-50",
                view.filter !== f ? "border-line bg-ink-800 text-fg-muted hover:text-fg" : "border-signal/50 bg-signal/15 text-signal",
              )}
            >
              {f === "all" ? "All" : f === "failing" ? "Failing" : "Multi-turn"}
            </button>
          ))}
          <span className="ml-2 font-mono text-[10.5px] text-fg-faint" aria-live="polite">
            {total == null ? "" : `${total.toLocaleString("en-US")} matching`}
          </span>
        </div>
        <input
          value={qText}
          onChange={(e) => setQText(e.target.value)}
          placeholder="Search input, answer, model, metadata, agent…"
          aria-label="Search conversations"
          className="w-64 rounded-lg border border-line bg-ink-800 px-3 py-1.5 text-[12.5px] text-fg placeholder:text-fg-faint focus:border-signal/40 focus:outline-none"
          suppressHydrationWarning
        />
      </div>

      {rows.length === 0 ? (
        <div className="card px-4 py-14 text-center text-[13px] text-fg-faint">
          {loading ? (
            "Loading…"
          ) : filtered ? (
            <>No conversations match these filters across the whole workspace{total === 0 ? "" : ` (${total ?? 0} counted)`} — clear a filter or widen the range.</>
          ) : (
            <>
              No traces yet — send one with the SDK or point an OTLP exporter at <code className="text-fg-muted">/v1/traces</code>.
            </>
          )}
        </div>
      ) : (
        <div className="reveal space-y-3" style={{ animationDelay: "80ms" }}>
          <TraceTable conversations={rows} onDeleted={onDeleted} sort={{ ...view.sortBy, onSort, busy: loading }} />
          <div className="flex items-center justify-center gap-3 pt-1">
            {hasMore ? (
              <button
                onClick={() => void fetchPage(view, rows.length, false)}
                disabled={loading}
                className="rounded-lg border border-line bg-ink-800 px-4 py-2 text-[12.5px] font-medium text-fg-muted transition-colors hover:text-fg disabled:opacity-50"
              >
                {loading ? "Loading…" : "Load more"}
              </button>
            ) : null}
            <span className="font-mono text-[10.5px] text-fg-faint">
              {rows.length} of {total ?? "?"} loaded
            </span>
            {shareUrl && (
              <a href={`?${shareUrl}`} className="font-mono text-[10.5px] text-fg-faint hover:text-signal" title="This URL reproduces the current view">
                link to this view
              </a>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
