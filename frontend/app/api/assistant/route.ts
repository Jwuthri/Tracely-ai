import { NextRequest } from "next/server";
import { authHeaders } from "@/app/lib/auth";

// Browser proxy for one chat turn. POST { message, chat_id?, attachments?, path } → an SSE
// stream of `data: <json>` frames (see the assistant router's docstring). Piped straight
// through, like the evaluation-run proxy: the turn runs tools and takes tens of seconds, so
// buffering it here would put the widget back where it started. The Bearer key stays server-side.
const API = process.env.TRACELY_API ?? "http://localhost:8000";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  const body = await req.json().catch(() => ({}));
  const upstream = await fetch(`${API}/api/assistant/chat`, {
    method: "POST",
    headers: {
      ...(await authHeaders()),
      "Content-Type": "application/json",
      accept: "text/event-stream",
    },
    cache: "no-store",
    body: JSON.stringify({
      message: body?.message ?? "",
      chat_id: body?.chat_id ?? null,
      attachments: body?.attachments ?? [],
      path: body?.path ?? "",
      context: body?.context ?? null,
    }),
    // @ts-expect-error — duplex is required by undici for streaming request/response pairs
    duplex: "half",
  });
  if (!upstream.ok || !upstream.body) {
    const text = await upstream.text();
    return new Response(text || JSON.stringify({ detail: "the assistant is unreachable" }), {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
    });
  }
  return new Response(upstream.body, {
    status: 200,
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
    },
  });
}
