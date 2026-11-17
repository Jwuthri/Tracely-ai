import { NextRequest } from "next/server";
import { forward } from "@/app/lib/forward";

type Ctx = { params: Promise<{ rolloutId: string }> };

// One rollout with its full transcript (every branch, the attacker's side, each judge's answer).
// Fetched on demand when a row is opened — it is hundreds of KB, far too much to inline per row.
export const GET = async (req: NextRequest, ctx: Ctx) =>
  forward(req, `/api/audit-rollouts/${encodeURIComponent((await ctx.params).rolloutId)}`);
