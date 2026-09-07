import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

// Browser proxy: per-row rolling summaries for a conversation's 3 table levels
// (conversation / per-trace / per-span).
const API = process.env.TRACELY_API ?? "http://localhost:8000";

export async function GET(_req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const r = await fetch(
    `${API}/api/sessions/${id}/rolling-summary/by-level`,
    { headers: await authHeaders(), cache: "no-store" },
  );
  return NextResponse.json(await r.json(), { status: r.status });
}
