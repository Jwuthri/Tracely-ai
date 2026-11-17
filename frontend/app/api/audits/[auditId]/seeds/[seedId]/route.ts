import { NextRequest } from "next/server";
import { forward } from "@/app/lib/forward";

type Ctx = { params: Promise<{ auditId: string; seedId: string }> };
const path = async (ctx: Ctx) => {
  const { auditId, seedId } = await ctx.params;
  return `/api/audits/${encodeURIComponent(auditId)}/seeds/${encodeURIComponent(seedId)}`;
};

export const PATCH = async (req: NextRequest, ctx: Ctx) => forward(req, await path(ctx));
export const DELETE = async (req: NextRequest, ctx: Ctx) => forward(req, await path(ctx));
