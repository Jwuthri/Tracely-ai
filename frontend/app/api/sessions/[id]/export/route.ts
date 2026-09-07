import { NextResponse } from "next/server";
import { authHeaders } from "@/app/lib/auth";

const API = process.env.TRACELY_API ?? "http://localhost:8000";

/** The whole conversation as one JSON object — what the table's copy button puts on the clipboard. */
export async function GET(_req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const r = await fetch(`${API}/api/sessions/${id}/export`, {
    headers: await authHeaders(),
    cache: "no-store",
  });
  // A backend that fell over answers in HTML, not JSON, and `r.json()` on that throws — turning a
  // readable 502 into an opaque 500 from this route. Pass the status through either way.
  const body = await r.text();
  try {
    return NextResponse.json(JSON.parse(body), { status: r.status });
  } catch {
    return NextResponse.json({ detail: body.slice(0, 300) || r.statusText }, { status: r.status });
  }
}
