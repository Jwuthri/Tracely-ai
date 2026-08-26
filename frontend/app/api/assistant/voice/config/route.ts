import { NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

// Which speech providers the deployment offers, and their voices. Same proxy shape as the
// other assistant routes: the Bearer key stays server-side.
const API = process.env.TRACELY_API ?? "http://localhost:8000";

export async function GET() {
  const r = await fetch(`${API}/api/assistant/voice/config`, {
    headers: await authHeaders(),
    cache: "no-store",
  });
  return NextResponse.json(await r.json().catch(() => ({})), { status: r.status });
}
