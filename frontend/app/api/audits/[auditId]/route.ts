import { NextRequest } from "next/server";
import { forward } from "@/app/lib/forward";

type Ctx = { params: Promise<{ auditId: string }> };
const path = async (ctx: Ctx) => `/api/audits/${encodeURIComponent((await ctx.params).auditId)}`;

export const PATCH = async (req: NextRequest, ctx: Ctx) => forward(req, await path(ctx));
export const DELETE = async (req: NextRequest, ctx: Ctx) => forward(req, await path(ctx));
