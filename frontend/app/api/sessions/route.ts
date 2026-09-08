import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

// Client-side proxy for the threads list — lets TracesExplorer page (offset) and re-query on a
// date-range change without a full server render. Mirrors lib/api.ts::getSessions but reachable from
// the browser. `from`/`to` are ISO-8601 (UTC); forwarded to the backend's from_ts/to_ts.
const API = process.env.TRACELY_API ?? "http://localhost:8000";

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const qs = new URLSearchParams({
    limit: sp.get("limit") ?? "50",
    offset: sp.get("offset") ?? "0",
  });
  const from = sp.get("from");
  const to = sp.get("to");
  if (from) qs.set("from_ts", from);
  if (to) qs.set("to_ts", to);
  // The Evals toggle: also list Tracely's own runs (evaluations, scenarios) as rows.
  if (sp.get("evals") === "1") qs.set("evals", "true");
  // Sortable column headers. Forwarded as-is — the backend whitelists the sort key, so an unknown
  // one falls back to the default order instead of reaching the query.
  const sort = sp.get("sort");
  const order = sp.get("order");
  if (sort) qs.set("sort", sort);
  if (order) qs.set("order", order);
  // The Agent select: a registry agent id, forwarded as-is.
  const agent = sp.get("agent");
  if (agent) qs.set("agent", agent);
  // Status / multi-turn / text filters run server-side over the whole thread set (W7).
  for (const k of ["failing", "multi", "q"] as const) {
    const v = sp.get(k);
    if (v) qs.set(k, v);
  }
  const r = await fetch(`${API}/api/sessions?${qs.toString()}`, {
    headers: await authHeaders(),
    cache: "no-store",
  });
  return NextResponse.json(await r.json(), { status: r.status });
}

// Multi-select delete from the traces table: { threads: [...] } → the backend's DELETE /api/sessions.
export async function DELETE(req: NextRequest) {
  const r = await fetch(`${API}/api/sessions`, {
    method: "DELETE",
    headers: { ...(await authHeaders()), "Content-Type": "application/json" },
    body: JSON.stringify(await req.json()),
    cache: "no-store",
  });
  return NextResponse.json(await r.json(), { status: r.status });
}
