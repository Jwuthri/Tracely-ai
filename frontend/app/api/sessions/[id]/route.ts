import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

// Client-side proxy: one thread's turns (+ conversation-level scores). Mirrors lib/api.ts::getSession
// but reachable from the browser — used by the Add-Column preview's turn picker.
const API = process.env.TRACELY_API ?? "http://localhost:8000";

export async function GET(_req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const r = await fetch(`${API}/api/sessions/${id}`, {
    headers: await authHeaders(),
    cache: "no-store",
  });
  return NextResponse.json(await r.json(), { status: r.status });
}
