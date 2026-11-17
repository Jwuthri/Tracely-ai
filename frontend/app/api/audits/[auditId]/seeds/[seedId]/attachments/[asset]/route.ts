import { NextRequest } from "next/server";
import { forward } from "@/app/lib/forward";

type Ctx = { params: Promise<{ auditId: string; seedId: string; asset: string }> };

export async function DELETE(req: NextRequest, ctx: Ctx) {
  const { auditId, seedId, asset } = await ctx.params;
  return forward(
    req,
    `/api/audits/${encodeURIComponent(auditId)}/seeds/${encodeURIComponent(seedId)}/attachments/${encodeURIComponent(asset)}`,
  );
}
