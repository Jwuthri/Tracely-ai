import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

// Mints one ephemeral voice-session token. The browser then connects to the provider directly
// (WebRTC for OpenAI, WebSocket for Grok) — this proxy only exists so TRACELY_KEY stays here.
const API = process.env.TRACELY_API ?? "http://localhost:8000";

export async function POST(req: NextRequest) {
  const r = await fetch(`${API}/api/assistant/voice/session`, {
    method: "POST",
    headers: { ...(await authHeaders()), "Content-Type": "application/json" },
    body: JSON.stringify(await req.json().catch(() => ({}))),
    cache: "no-store",
  });
  return NextResponse.json(await r.json().catch(() => ({})), { status: r.status });
}
