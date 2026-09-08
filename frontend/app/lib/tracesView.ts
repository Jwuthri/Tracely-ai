/**
 * The traces list's view — filters, range, sort, text — and its URL form. Plain module (no
 * "use client"): the server page parses the URL with it and the client explorer syncs with it.
 */
import type { SessionSort, SessionsQuery, SortOrder } from "./api";

export type Filter = "all" | "failing" | "multi";
export type Range = { from: string | null; to: string | null }; // ISO-8601 (UTC); null = unbounded
export type SortBy = { sort: SessionSort; order: SortOrder };

export const DEFAULT_SORT: SortBy = { sort: "recent", order: "desc" };
export const SORTS: SessionSort[] = ["recent", "started", "duration", "tokens"];

export const PRESETS: { key: string; label: string; hours: number | null }[] = [
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

