import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

const API = process.env.TRACELY_API ?? "http://localhost:8000";

type Ctx = { params: Promise<{ asset: string }> };

// An audit's image or audio file, by content hash. Bytes, so it cannot go through `forward`
// (which speaks JSON). The backend only ever serves sniffed image/audio types from here.
export async function GET(_req: NextRequest, ctx: Ctx) {
  const { asset } = await ctx.params;
  if (!/^[0-9a-f]{64}$/.test(asset)) return NextResponse.json({ detail: "not found" }, { status: 404 });
  const r = await fetch(`${API}/api/audit-assets/${asset}`, { headers: await authHeaders(), cache: "no-store" });
  if (!r.ok) return NextResponse.json({ detail: "not found" }, { status: 404 });
  return new NextResponse(r.body, {
    headers: {
      "content-type": r.headers.get("content-type") ?? "application/octet-stream",
      "cache-control": "private, max-age=31536000, immutable",
      "x-content-type-options": "nosniff",
    },
  });
}
