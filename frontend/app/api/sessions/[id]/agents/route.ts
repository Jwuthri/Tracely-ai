import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

// Browser proxy: a conversation's agents + the tools each used (derived from its spans).
const API = process.env.TRACELY_API ?? "http://localhost:8000";

export async function GET(_req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const r = await fetch(`${API}/api/sessions/${id}/agents`, {
    headers: await authHeaders(),
    cache: "no-store",
  });
  return NextResponse.json(await r.json(), { status: r.status });
}
