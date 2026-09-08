import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

// Client-side proxy for editing a case's expected behaviour (the case page's editor).
const API = process.env.TRACELY_API ?? "http://localhost:8000";

export async function PATCH(req: NextRequest, { params }: { params: Promise<{ caseId: string }> }) {
  const { caseId } = await params;
  const r = await fetch(`${API}/api/cases/${encodeURIComponent(caseId)}/expectations`, {
    method: "PATCH",
    headers: { ...(await authHeaders()), "content-type": "application/json" },
    body: JSON.stringify(await req.json()),
    cache: "no-store",
  });
  return NextResponse.json(await r.json().catch(() => ({})), { status: r.status });
}
