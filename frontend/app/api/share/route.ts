import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

const API = process.env.TRACELY_API ?? "http://localhost:8000";

// Mint / revoke proxy. Reading a shared object does NOT come through here — that path is
// anonymous and server-rendered by /share/[token].
export async function POST(req: NextRequest) {
  const { threadId, kind, id } = await req.json();
  const r = await fetch(`${API}/api/share`, {
    method: "POST",
    headers: { ...(await authHeaders()), "Content-Type": "application/json" },
    body: JSON.stringify({ kind: kind ?? "conversation", id: id ?? threadId }),
  });
  const data = await r.json();
  return NextResponse.json(data, { status: r.status });
}

// "Stop sharing this": kills every link ever minted for the subject, not just the one in hand —
// an owner who no longer has a copy of the link still has to be able to pull it back.
export async function DELETE(req: NextRequest) {
  const kind = req.nextUrl.searchParams.get("kind") ?? "conversation";
  const id = req.nextUrl.searchParams.get("id") ?? "";
  const r = await fetch(`${API}/api/share/revoke`, {
    method: "POST",
    headers: { ...(await authHeaders()), "Content-Type": "application/json" },
    body: JSON.stringify({ kind, id }),
  });
  const data = await r.json();
  return NextResponse.json(data, { status: r.status });
}
