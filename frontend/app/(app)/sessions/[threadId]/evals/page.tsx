import {
  getChainProgress,
  getSession,
  getTrace,
  type ChainMetric,
  type ConvNode,
  type FullTurn,
} from "@/app/lib/api";
import { CopyId } from "@/app/components/CopyId";
import { EvalLevelView } from "@/app/components/EvalLevelView";
import { LEVELS, type EvalLevel } from "@/app/components/eval-levels";
import { IconArrowLeft } from "@/app/components/icons";

// How Tracely graded ONE conversation, split the way the evaluators are: one tab per level.
//
// These runs used to sit in /traces behind an "Evals" chip, where 12 conversations showed up as 42
// extra rows you had to read the title of to know what they graded. They belong to the conversation
// they are about, so that is where they are now — and the recording writes one thread per level
// (`eval:<thread>:step|msg|conv`), which is exactly the three tabs. Batch and sequential columns
// share a level, so they share a tab; the Agent column names the column each row came from.
export default async function ConversationEvalsPage({
  params,
}: {
  params: Promise<{ threadId: string }>;
}) {
  const { threadId } = await params;
  const thread = decodeURIComponent(threadId);
  const [chain, ...levels] = await Promise.all([
    getChainProgress(encodeURIComponent(thread)).catch(() => ({ metrics: [] as ChainMetric[] })),
    ...LEVELS.map((level) => loadLevel(thread, level.key)),
  ]);
  const found = levels.filter((l): l is NonNullable<typeof l> => l !== null);

  return (
    <div className="space-y-6">
      <header className="reveal">
        <a
          href={`/sessions/${encodeURIComponent(thread)}`}
          className="inline-flex items-center gap-1.5 text-[13px] text-fg-muted transition-colors hover:text-signal"
        >
          <IconArrowLeft className="h-4 w-4" /> Conversation
        </a>
        <h1 className="mt-4 font-display text-[22px] font-extrabold tracking-tight">Evaluations</h1>
        <div className="mt-2 flex flex-wrap items-center gap-3 font-mono text-[11.5px] text-fg-faint">
          <CopyId value={thread} label="thread id" />
          <span>
            {found.reduce((a, l) => a + l.turns.length, 0)} run
            {found.reduce((a, l) => a + l.turns.length, 0) === 1 ? "" : "s"}
          </span>
        </div>
      </header>

      {chain.metrics.length > 0 && (
        <div className="card reveal p-4" style={{ animationDelay: "30ms" }}>
          <div className="text-[11px] font-semibold uppercase tracking-wider text-fg-faint">
            Sequential chains
          </div>
          <p className="mt-1 text-[12px] text-fg-muted">
            Each sequential column grades this conversation as one running dialogue with the
            judge — new turns are appended as they arrive.
          </p>
          <div className="mt-3 space-y-1.5">
            {chain.metrics.map((m) => (
              <ChainRow key={m.score_name} m={m} />
            ))}
          </div>
        </div>
      )}

      {found.length === 0 ? (
        <div className="card p-10 text-center text-[13px] text-fg-faint">
          Nothing graded this conversation yet — run the evaluators from the conversation page.
        </div>
      ) : (
        <div className="reveal" style={{ animationDelay: "60ms" }}>
          <EvalLevelView levels={found} />
        </div>
      )}
    </div>
  );
}

/** One sequential column's chain state: how far its judge conversation is through the thread. */
function ChainRow({ m }: { m: ChainMetric }) {
  const level = m.level === "AGENT_RUN" ? "msg" : "step";
  const behind = m.turns - m.chained;
  const payload = m.last_payload ?? {};
  const verdict = typeof payload.verdict === "string" ? payload.verdict : "";
  const value = [payload.value, payload.score].find((v) => typeof v === "number") as
    | number
    | undefined;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-line bg-ink-800/40 px-3 py-2 font-mono text-[11.5px]">
      <span className="text-fg">{m.score_name}</span>
      <span className="rounded border border-line px-1 py-px text-[9px] uppercase tracking-wider text-fg-faint">
        {level}
      </span>
      <span className="text-fg-muted">
        {m.chained}/{m.turns} turns
      </span>
      {m.up_to_date ? (
        <span className="text-ok">up to date</span>
      ) : (
        <span className="text-warn">
          {m.chained === 0 ? "not started" : `${behind} behind`}
        </span>
      )}
      {(verdict === "PASS" || verdict === "FAIL") && (
        <span
          className={
            verdict === "PASS"
              ? "rounded border border-ok/30 bg-ok/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-ok"
              : "rounded border border-fail/30 bg-fail/10 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-fail"
          }
        >
          last: {verdict}
        </span>
      )}
      {typeof value === "number" && <span className="text-fg-muted">{value.toFixed(2)}</span>}
      {m.updated_at && (
        <span className="ml-auto text-[10.5px] text-fg-faint">
          {m.updated_at.slice(0, 16).replace("T", " ")}
        </span>
      )}
    </div>
  );
}

/** One level's eval runs as a conversation, or null when that level graded nothing. */
async function loadLevel(
  threadId: string,
  key: EvalLevel,
): Promise<{ key: EvalLevel; conv: ConvNode; turns: FullTurn[] } | null> {
  const evalThread = `eval:${threadId}:${key}`;
  const { turns } = await getSession(encodeURIComponent(evalThread));
  if (turns.length === 0) return null;
  const traces = await Promise.all(turns.map((t) => getTrace(t.trace_id)));
  const fullTurns: FullTurn[] = turns.map((t, i) => ({ ...t, spans: traces[i].spans }));
  return {
    key,
    turns: fullTurns,
    conv: {
      thread: evalThread,
      turns: turns.length,
      first_input: turns[0]?.input ?? null,
      last_output: turns[turns.length - 1]?.output ?? null,
      tokens: turns.reduce((a, t) => a + (t.tokens || 0), 0),
      cost: turns.reduce((a, t) => a + (t.cost || 0), 0),
      first_ts: turns[0]?.ts ?? "",
      last_ts: turns[turns.length - 1]?.ts ?? "",
      last_trace_id: turns[turns.length - 1]?.trace_id ?? evalThread,
      failing: 0,
      turnsData: fullTurns,
      scores: [],
    },
  };
}
