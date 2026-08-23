// One assistant turn, decoded from the SSE stream the chat endpoint answers with.
// Same wire format as the evaluation run (`data: <json>` lines, `data: [DONE]` terminator) —
// see `backend/tracely/api/routers/assistant.py` for the frame protocol.

export type AssistantEvent =
  | { type: "tool"; name: string; args: Record<string, unknown> }
  | { type: "tool_done"; name: string; ok: boolean }
  | { type: "delta"; text: string }
  | { type: "done"; chat_id: string; title: string; reply: string }
  | { type: "disabled" }
  | { type: "over_budget"; spent_usd: number; budget_usd: number }
  | { type: "error"; detail: string };

export type TurnRequest = {
  message: string;
  chat_id: string | null;
  attachments: { id: string; name: string; mime: string; size: number }[];
  path: string;
  /** What the current page shares about itself (see `pageContext.ts`); this turn only. */
  context?: unknown;
};

// POST the turn and invoke `onEvent` per frame. Resolves when the stream ends; rejects on a
// non-2xx response (a turn that fails mid-stream arrives as an `error` frame instead, because
// the status code is already 200 by the time anything can go wrong).
export async function streamAssistantTurn(
  body: TurnRequest,
  onEvent: (e: AssistantEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch("/api/assistant", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      const payload = await res.json();
      if (payload?.detail) detail = String(payload.detail);
    } catch {
      /* keep statusText */
    }
    throw new Error(detail || "the assistant is unreachable");
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? ""; // a frame split across chunks finishes on the next read
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const payload = line.slice("data: ".length).trim();
      if (payload === "[DONE]") return;
      try {
        onEvent(JSON.parse(payload) as AssistantEvent);
      } catch {
        /* skip malformed frame */
      }
    }
  }
}

// "get_trace" → "get trace" — how a tool call is named in the activity log. The tool names are
// written to be read, so the only work here is undoing the underscores.
export function toolLabel(name: string): string {
  return name.replace(/_/g, " ");
}
