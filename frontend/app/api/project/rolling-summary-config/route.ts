import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

// The workspace's rolling-summary budget (what backs @HISTORY / @ROLLING_SUMMARY). Same proxy
// shape as ui-prefs: the browser never sees TRACELY_KEY/TRACELY_API.
const API = process.env.TRACELY_API ?? "http://localhost:8000";

export async function GET() {
  const r = await fetch(`${API}/api/project/rolling-summary-config`, {
    headers: await authHeaders(),
    cache: "no-store",
  });
  return NextResponse.json(await r.json().catch(() => ({})), { status: r.status });
}

export async function PUT(req: NextRequest) {
  const r = await fetch(`${API}/api/project/rolling-summary-config`, {
    method: "PUT",
    headers: { ...(await authHeaders()), "Content-Type": "application/json" },
    body: JSON.stringify(await req.json().catch(() => ({}))),
    cache: "no-store",
  });
  return NextResponse.json(await r.json().catch(() => ({})), { status: r.status });
}
