import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

const API = process.env.TRACELY_API ?? "http://localhost:8000";

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ agentRef: string }> },
) {
  const { agentRef } = await params;
  const r = await fetch(`${API}/api/agents/${agentRef}/endpoint`, {
    headers: await authHeaders(),
    cache: "no-store",
  });
  return NextResponse.json(await r.json(), { status: r.status });
}

/** The token travels browser → here → backend and is Fernet-encrypted at rest; the GET above
 *  only ever returns `has_token`, so it never comes back out. */
export async function PUT(req: NextRequest, { params }: { params: Promise<{ agentRef: string }> }) {
  const { agentRef } = await params;
  const body = await req.json();
  const r = await fetch(`${API}/api/agents/${agentRef}/endpoint`, {
    method: "PUT",
    headers: { ...(await authHeaders()), "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return NextResponse.json(await r.json(), { status: r.status });
}
