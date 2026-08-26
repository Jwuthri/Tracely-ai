import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

const API = process.env.TRACELY_API ?? "http://localhost:8000";

export async function POST(req: NextRequest) {
  const { clusterId, action } = await req.json();
  const act = ["ignore", "unpromote"].includes(action) ? action : "promote";
  const r = await fetch(`${API}/api/clusters/${clusterId}/${act}`, {
    method: "POST",
    headers: await authHeaders(),
  });
  const data = await r.json();
  return NextResponse.json(data, { status: r.status });
}
