import { NextRequest } from "next/server";
import { forward } from "@/app/lib/forward";

type Ctx = { params: Promise<{ auditId: string }> };

// Start a run. Returns the run id straight away — the worker advances it in short slices.
export const POST = async (req: NextRequest, ctx: Ctx) =>
  forward(req, `/api/audits/${encodeURIComponent((await ctx.params).auditId)}/runs`);
