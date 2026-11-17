import { NextRequest } from "next/server";
import { forward } from "@/app/lib/forward";

type Ctx = { params: Promise<{ auditId: string }> };

// Write a batch of new seeds from the audit's behaviour (two model calls; takes a few seconds).
export const POST = async (req: NextRequest, ctx: Ctx) =>
  forward(req, `/api/audits/${encodeURIComponent((await ctx.params).auditId)}/seeds/generate`);
