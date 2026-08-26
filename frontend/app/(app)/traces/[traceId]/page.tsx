import { getCaseForTrace, getTrace, type ConvNode, type FullTurn } from "@/app/lib/api";
import { convUsage, fmtUsd } from "@/app/lib/usage";
import { PromoteButton } from "@/app/components/PromoteButton";
import { DeleteCaseButton } from "@/app/components/DeleteCaseButton";
import { SingleTraceView } from "@/app/components/SingleTraceView";
import { CopyId } from "@/app/components/CopyId";
import { Badge } from "@/app/components/ui";
import { IconArrowLeft } from "@/app/components/icons";

export default async function TracePage({ params }: { params: Promise<{ traceId: string }> }) {
  const { traceId } = await params;
  const [{ spans, scores, eval_verdict, thread_id }, promotedCase] = await Promise.all([
    getTrace(traceId),
    getCaseForTrace(traceId),
  ]);
  const hasError = spans.some((s) => s.level === "ERROR");
  const failing = hasError || eval_verdict === "FAIL";
  const root = spans.find((s) => s.parent_span_id === "") ?? spans[0];

  const t0 = spans.length ? Math.min(...spans.map((s) => new Date(s.start_time).getTime())) : 0;
  const t1 = spans.length ? Math.max(...spans.map((s) => new Date(s.end_time ?? s.start_time).getTime())) : 0;
  const totalMs = Math.max(t1 - t0, 0);
  const totalTokens = spans.reduce((a, s) => a + (s.tokens || 0), 0);
  const totalCost = spans.reduce((a, s) => a + (s.cost || 0), 0);

  // Derive the user/assistant text for the turn from the run's root span.
  const input = root?.input ?? spans.find((s) => s.input)?.input ?? null;
  // Prefer the LAST GENERATION output (the model's real reply), then root, then any non-TOOL/non-
  // CHAIN span. Skipping CHAIN avoids framework routing signals (LangGraph's `tools_condition`
  // outputs `"__end__"`) being shown as the assistant's answer.
  const reversed = [...spans].reverse();
  const output =
    reversed.find((s) => s.type === "GENERATION" && s.output)?.output ??
    root?.output ??
    reversed.find((s) => s.output && s.type !== "TOOL" && s.type !== "CHAIN")?.output ??
    null;

  const turn: FullTurn = {
    trace_id: traceId,
    input,
    output,
    tokens: totalTokens,
    cost: totalCost,
    latency_ms: totalMs,
    ts: root?.start_time ?? "",
    failing: failing ? 1 : 0,
    scores,
    verdict: eval_verdict,
    spans,
  };
  const conv: ConvNode = {
    // the real conversation thread (== traceId for a 1-turn thread) so conversation-level
    // metric columns resolve and thread-scoped eval runs target the right rows
    thread: thread_id || traceId,
    turns: 1,
    first_input: input,
    last_output: output,
    tokens: totalTokens,
    cost: totalCost,
    first_ts: root?.start_time ?? "",
    // last_ts must be the trace's END (latest span end), not the root's start — otherwise the
    // conversation row's duration (last_ts − first_ts) is always 0 and renders as "—".
    last_ts:
      spans.reduce((acc, s) => {
        const e = s.end_time ?? s.start_time;
        return new Date(e).getTime() > new Date(acc).getTime() ? e : acc;
      }, root?.start_time ?? "") || (root?.start_time ?? ""),
    last_trace_id: traceId,
    failing: failing ? 1 : 0,
    turnsData: [turn],
    scores: scores.filter((s) => s.evaluation_level === "CONVERSATION"),
  };
  const usage = convUsage(conv);

  return (
    <div className="space-y-6">
      <header className="reveal space-y-4">
        <a href="/traces" className="inline-flex items-center gap-1.5 text-[13px] text-fg-muted transition-colors hover:text-signal">
          <IconArrowLeft className="h-4 w-4" /> Traces
        </a>
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <div className="flex items-center gap-3">
              <h1 className="font-display text-[24px] font-extrabold tracking-tight">{root?.name ?? "trace"}</h1>
              {failing ? (
                <Badge variant="fail" dot>{hasError ? "error" : "failing"}</Badge>
              ) : (
                <Badge variant="ok" dot>ok</Badge>
              )}
            </div>
            <div className="mt-2.5 flex flex-wrap items-center gap-3 font-mono text-[11.5px] text-fg-faint">
              <CopyId value={traceId} label="trace id" />
              <span>{spans.length} spans</span>
              <span>{totalMs < 1000 ? `${Math.round(totalMs)}ms` : `${(totalMs / 1000).toFixed(2)}s`}</span>
              {usage.input_tokens ? <span>{usage.input_tokens.toLocaleString("en-US")} in</span> : null}
              {usage.cached_tokens ? <span>{usage.cached_tokens.toLocaleString("en-US")} cached</span> : null}
              {usage.output_tokens ? <span>{usage.output_tokens.toLocaleString("en-US")} out</span> : null}
              {usage.total_tokens ? <span>{usage.total_tokens.toLocaleString("en-US")} tokens</span> : null}
              {usage.cost ? <span className="text-warn/90">{fmtUsd(usage.cost)}</span> : null}
            </div>
          </div>
          <div className="flex items-center gap-3">
            {/* Watch this run act itself out. `thread_spans_full` accepts a TRACE id as well as a
                conversation id, so a one-turn run replays fine — but prefer the thread when the
                run belongs to a conversation, so the replay covers every turn. */}
            {/* only when the turn belongs to a real conversation thread */}
            {thread_id && (
              <a href={`/sessions/${encodeURIComponent(thread_id)}`} className="btn-ghost">
                ← Conversation
              </a>
            )}
            <a href={`/sessions/${encodeURIComponent(thread_id || traceId)}/replay`} className="btn-ghost">
              ▶ Replay
            </a>
            <a href={`/sessions/${encodeURIComponent(thread_id || traceId)}/fleet`} className="btn-ghost">
              ⌂ Fleet
            </a>
            {promotedCase ? (
              <>
                <a href={`/cases/${promotedCase.id}`} className="btn-ghost">
                  ⚑ Case
                </a>
                <DeleteCaseButton
                  caseId={promotedCase.id}
                  title={promotedCase.title}
                  label="Remove from regression"
                  redirectTo={null}
                />
              </>
            ) : (
              failing && <PromoteButton traceId={traceId} />
            )}
          </div>
        </div>
      </header>

      <SingleTraceView conv={conv} spans={spans} verdict={eval_verdict} />
    </div>
  );
}
