import { NextRequest } from "next/server";
import { forward } from "@/app/lib/forward";

type Ctx = { params: Promise<{ auditId: string }> };

export const POST = async (req: NextRequest, ctx: Ctx) =>
  forward(req, `/api/audits/${encodeURIComponent((await ctx.params).auditId)}/seeds`);
