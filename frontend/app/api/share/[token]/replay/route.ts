import { NextRequest, NextResponse } from "next/server";

const API = process.env.TRACELY_API ?? "http://localhost:8000";

// Anonymous, token-scoped read: the share page's fleet replay (a Client Component) fetches
// through here. The token in the path is the credential — no auth headers, on purpose.
// Listed in middleware.ts PUBLIC alongside /share/.
export async function GET(_req: NextRequest, ctx: { params: Promise<{ token: string }> }) {
  const { token } = await ctx.params;
  const r = await fetch(`${API}/api/share/${encodeURIComponent(token)}/replay`, { cache: "no-store" });
  const data = await r.json().catch(() => ({}));
  return NextResponse.json(data, { status: r.status });
}
