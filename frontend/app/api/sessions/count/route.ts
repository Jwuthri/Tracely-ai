import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

// The count for the same filters /api/sessions takes — one number the list, its empty state and
// its pages all agree on.
const API = process.env.TRACELY_API ?? "http://localhost:8000";

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const qs = new URLSearchParams();
  const from = sp.get("from");
  const to = sp.get("to");
  if (from) qs.set("from_ts", from);
  if (to) qs.set("to_ts", to);
  if (sp.get("evals") === "1") qs.set("evals", "true");
  const agent = sp.get("agent");
  if (agent) qs.set("agent", agent);
  for (const k of ["failing", "multi", "q"] as const) {
    const v = sp.get(k);
    if (v) qs.set(k, v);
  }
  const r = await fetch(`${API}/api/sessions/count?${qs.toString()}`, { headers: await authHeaders(), cache: "no-store" });
  return NextResponse.json(await r.json().catch(() => ({ total: 0 })), { status: r.status });
}
