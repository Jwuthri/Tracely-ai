import { NextRequest, NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

const API = process.env.TRACELY_API ?? "http://localhost:8000";

/** Re-issue a browser request against the backend with the caller's credentials attached
 *  server-side — the whole job of a route under `app/api/*`. SERVER ONLY: this module reads
 *  `TRACELY_API`, which must never reach the browser, so it is imported by route handlers and
 *  nothing else.
 *
 *  A non-JSON error body (a proxy's HTML 502) becomes `{detail}` so every caller can show
 *  `d.detail` without guarding the parse. */
export async function forward(req: NextRequest, path: string): Promise<NextResponse> {
  const hasBody = req.method !== "GET" && req.method !== "DELETE";
  const body = hasBody ? (await req.text()) || "{}" : undefined;
  const r = await fetch(`${API}${path}`, {
    method: req.method,
    headers: { ...(await authHeaders()), ...(hasBody ? { "content-type": "application/json" } : {}) },
    body,
    cache: "no-store",
  });
  const data = await r.json().catch(() => ({ detail: `The server answered HTTP ${r.status}.` }));
  return NextResponse.json(data, { status: r.status });
}
