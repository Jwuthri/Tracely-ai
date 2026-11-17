import { NextRequest, NextResponse } from "next/server";
import { forward } from "@/app/lib/forward";

type Ctx = { params: Promise<{ auditId: string; seedId: string; action: string }> };

// `variations` writes one variation of the seed per axis; `attachments` adds an image or audio
// file to it (base64 in the JSON body — the backend sniffs the type from the bytes).
export async function POST(req: NextRequest, ctx: Ctx) {
  const { auditId, seedId, action } = await ctx.params;
  if (action !== "variations" && action !== "attachments") {
    return NextResponse.json({ detail: "unknown action" }, { status: 404 });
  }
  return forward(req, `/api/audits/${encodeURIComponent(auditId)}/seeds/${encodeURIComponent(seedId)}/${action}`);
}
