import { getSession, getTrace, type ConvNode, type FullTurn } from "./api";
import { convUsage } from "./usage";

/** Everything the conversation CHROME needs, loaded once. Table, Timeline, Replay and Fleet are
 *  four lenses on the same thread, so they all load it the same way and wear the same header —
 *  a lens change must not look like a different page. */
export type Conversation = {
  conv: ConvNode;
  turns: FullTurn[];
  usage: Record<string, number>;
  /** Agent to attach a promoted scenario to; "" when no span carries one (→ no button). */
  agentRef: string;
  spans: number;
  verdict: "PASS" | "FAIL" | null;
};

export async function loadConversation(threadId: string): Promise<Conversation> {
  const { turns, scores: threadScores } = await getSession(encodeURIComponent(threadId));
  // Eagerly resolve each turn's spans so the whole tree renders pre-expanded.
  const traces = await Promise.all(turns.map((t) => getTrace(t.trace_id)));
  const fullTurns: FullTurn[] = turns.map((t, i) => ({ ...t, spans: traces[i].spans }));
  const conv: ConvNode = {
    thread: threadId,
    turns: turns.length,
    first_input: turns[0]?.input ?? null,
    last_output: turns[turns.length - 1]?.output ?? null,
    tokens: turns.reduce((a, t) => a + (t.tokens || 0), 0),
    cost: turns.reduce((a, t) => a + (t.cost || 0), 0),
    first_ts: turns[0]?.ts ?? "",
    last_ts: turns[turns.length - 1]?.ts ?? "",
    last_trace_id: turns[turns.length - 1]?.trace_id ?? threadId,
    failing: turns.some((t) => t.failing === 1 || t.verdict === "FAIL") ? 1 : 0,
    turnsData: fullTurns,
    scores: threadScores ?? [],
  };
  return {
    conv,
    turns: fullTurns,
    usage: convUsage(conv),
    agentRef: traces.flatMap((t) => t.spans).find((s) => s.agent_id)?.agent_id ?? "",
    spans: traces.reduce((n, t) => n + t.spans.length, 0),
    verdict: conversationVerdict(conv, fullTurns),
  };
}

/** One verdict for the whole thread: FAIL if anything failed, PASS if anything was graded at
 *  all, null when nothing has been. Mirrors the backend's non-advisory policy. */
export function conversationVerdict(conv: ConvNode, turns: FullTurn[]): "PASS" | "FAIL" | null {
  const graded = turns.reduce((a, t) => a + t.scores.length, 0) + (conv.scores?.length ?? 0);
  const failed =
    turns.some((t) => t.verdict === "FAIL" || t.failing === 1) ||
    (conv.scores ?? []).some((s) => s.verdict === "FAIL");
  return failed ? "FAIL" : graded > 0 ? "PASS" : null;
}
