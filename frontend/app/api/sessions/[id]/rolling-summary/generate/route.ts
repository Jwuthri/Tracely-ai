import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

// Browser proxy: generate (or force-refresh) a conversation's rolling summary.
const API = process.env.TRACELY_API ?? "http://localhost:8000";

export async function POST(req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const body = await req.json().catch(() => ({}));
  const r = await fetch(
    `${API}/api/sessions/${id}/rolling-summary/generate`,
    {
      method: "POST",
      headers: { ...(await authHeaders()), "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify({ force: !!body?.force }),
    },
  );
  return NextResponse.json(await r.json(), { status: r.status });
}
