import { NextRequest, NextResponse } from "next/server";
import { forward } from "@/app/lib/forward";

type Ctx = { params: Promise<{ runId: string; action: string }> };

// `cancel` stops a run; `resume` nudges one whose worker went away.
export async function POST(req: NextRequest, ctx: Ctx) {
  const { runId, action } = await ctx.params;
  if (action !== "cancel" && action !== "resume") {
    return NextResponse.json({ detail: "unknown action" }, { status: 404 });
  }
  return forward(req, `/api/audit-runs/${encodeURIComponent(runId)}/${action}`);
}
