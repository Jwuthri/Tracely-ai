import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

// Workspace UI defaults (hidden step types, …). Same proxy shape as llm-key: the browser
// never sees TRACELY_KEY/TRACELY_API; requests re-issue here with auth attached server-side.
const API = process.env.TRACELY_API ?? "http://localhost:8000";

export async function GET() {
  const r = await fetch(`${API}/api/project/ui-prefs`, {
    headers: await authHeaders(),
    cache: "no-store",
  });
  return NextResponse.json(await r.json().catch(() => ({})), { status: r.status });
}

export async function PUT(req: NextRequest) {
  const r = await fetch(`${API}/api/project/ui-prefs`, {
    method: "PUT",
    headers: { ...(await authHeaders()), "Content-Type": "application/json" },
    body: JSON.stringify(await req.json().catch(() => ({}))),
    cache: "no-store",
  });
  return NextResponse.json(await r.json().catch(() => ({})), { status: r.status });
}
