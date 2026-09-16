import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

// Client-side lazy-load proxy: the judge prompt + reply behind one score cell.
const API = process.env.TRACELY_API ?? "http://localhost:8000";

export async function GET(req: NextRequest) {
  const qs = req.nextUrl.searchParams.toString();
  const r = await fetch(`${API}/api/evaluations/prompt?${qs}`, {
    headers: await authHeaders(),
    cache: "no-store",
  });
  return NextResponse.json(await r.json(), { status: r.status });
}
