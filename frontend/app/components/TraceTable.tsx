"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  ConvNode,
  SpanOut,
} from "../lib/api";
import {
  deleteColumnError,
  deleteEvaluator,
  levelGroup,
  listEvaluators,
  getEvaluatorCost,
  streamEvaluationRun,
  type EvaluatorDef,
  type EvaluatorCost,
  type RunScope,
} from "../lib/evaluators";
import { useHiddenTypes } from "../lib/typePrefs";
import { useWide, WideToggle, WIDE_STYLE } from "../lib/useWide";
import { AddColumnModal } from "./AddColumnModal";
import { AgentsSidePanel } from "./AgentsSidePanel";
import { spanHasState } from "./state-fold";
import { useConversationTree } from "./trace-table/useConversationTree";
import {
  scoreKey,
} from "./trace-table/format";
import {
  type Col,
  COLUMNS,
  type Group,
  KNOWN_SPAN_TYPES,
  METRIC_TINTS,
  PREFS_KEY,
} from "./trace-table/columns";
import {
  ChevronsUpDown,
  Eye,
  FilterIcon,
  Play,
  PlusIcon,
  Trash,
} from "./trace-table/icons";
import {
  normalizeType,
} from "./ui";
import {
  CTRL_CELLS,
  EvalViewContext,
  LiveScoreContext,
  RollingSummaryContext,
  SelectContext,
  useLiveScoreStore,
  type EvalView,
  type RollingSummaryView,
  type SelectView,
  type SummaryItems,
} from "./trace-table/contexts";
import { ColumnsMenu, HeaderRow, TypesMenu, type SortHandle } from "./trace-table/header";
import { ConvRows } from "./trace-table/rows";

// ── A TurnWise-style hierarchical spreadsheet over Tracely's real tree ─────────
//   Conversation (thread)  →  Message (turn, split user / assistant)  →  Step (span)
// A real <table> with C / M / S column groups, depth-coloured rows, inline JSON +
// usage pills (→ floating popover) and smart multimodal message content. Two modes:
//   • "list"   — conversation summaries; turns + spans lazy-load on expand.
//   • "detail" — full tree seeded (turnsData populated); everything pre-open.

// ── lucide-style icons ─────────────────────────────────────────────────────────
// Icons + the shared `svg()` helper live in ./trace-table/icons.tsx (imported above).

// Column shape + constants + KNOWN_SPAN_TYPES live in ./trace-table/columns.ts (imported above).

// ── format helpers ──────────────────────────────────────────────────────────────
// usage / cost derivation (spanUsage / turnUsage / convUsage / usageSummary / fmtUsd) lives in
// ../lib/usage so the detail-page headers can reuse the exact same logic.

// ── root ────────────────────────────────────────────────────────────────────────

/** Sorting is owned by whoever runs the query, because it happens server-side across the whole
 *  list rather than over the loaded page. The table only renders the affordance and reports clicks. */
export type { SortHandle } from "./trace-table/header";

export function TraceTable({
  conversations,
  embedded = false,
  shared = false,
  onDeleted,
  sort,
}: {
  conversations: ConvNode[];
  // When embedded in a tabbed trace view, the parent owns the Enlarge/Concise control + the
  // full-width breakout (so it applies across Table/Timeline/Evaluations), so we suppress ours.
  embedded?: boolean;
  // Public share page: hide every control that mutates project state (the visitor has no session,
  // so those calls would 401 anyway). Read-only controls — expand, filter, column visibility —
  // stay, because they are what makes a shared trace worth reading.
  shared?: boolean;
  // Passing this enables conversation multi-select + Delete; the parent drops the rows it gets back.
  onDeleted?: (threads: string[]) => void;
  // Passing this makes the C-group headers clickable. Omitted (embedded/shared views) they render
  // as plain labels — those views show one conversation, so there is nothing to order.
  sort?: SortHandle;
}) {
  // What is open and what has been fetched — see trace-table/useConversationTree.ts.
  const { turns, spans, openConv, openTurn, toggleConv, toggleTurn, toggleAll, allOpen } =
    useConversationTree(conversations);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const {
    hidden: hiddenTypes, source: typesSource, toggle: toggleType, reset: resetTypes,
    useWorkspaceDefault, saveAsWorkspaceDefault,
  } = useHiddenTypes();
  const [colMenu, setColMenu] = useState(false);
  const [typeMenu, setTypeMenu] = useState(false);
  const [wide, setWide] = useWide();
  const [agentsThread, setAgentsThread] = useState<string | null>(null);

  // ── conversation multi-select + delete ────────────────────────────────────────
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState(false);
  const selectView = useMemo<SelectView>(
    () => ({
      enabled: !!onDeleted,
      selected,
      toggle: (thread) =>
        setSelected((prev) => {
          const next = new Set(prev);
          if (!next.delete(thread)) next.add(thread);
          return next;
        }),
      // Header box: select every visible conversation, or clear when they're all already picked.
      toggleAll: () =>
        setSelected((prev) =>
          prev.size >= conversations.length ? new Set() : new Set(conversations.map((c) => c.thread)),
        ),
      allSelected: conversations.length > 0 && selected.size >= conversations.length,
      someSelected: selected.size > 0,
    }),
    [onDeleted, selected, conversations],
  );

  async function deleteSelected() {
    const threads = [...selected];
    if (!threads.length) return;
    if (!window.confirm(`Delete ${threads.length} conversation${threads.length === 1 ? "" : "s"}? Their traces, steps and scores are removed permanently.`)) return;
    setDeleting(true);
    try {
      const r = await fetch("/api/sessions", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ threads }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail ?? "delete failed");
      setSelected(new Set());
      onDeleted?.(threads);
    } catch (e) {
      setRunError(e instanceof Error ? e.message : "delete failed");
    } finally {
      setDeleting(false);
    }
  }

  // ── evaluation columns: definitions + live run state ──────────────────────────
  const [evaluators, setEvaluators] = useState<EvaluatorDef[]>([]);
  const [evalCost, setEvalCost] = useState<Record<string, EvaluatorCost>>({});
  const liveStore = useLiveScoreStore();
  const [busyCols, setBusyCols] = useState<Set<string>>(new Set());
  const [busyRows, setBusyRows] = useState<Set<string>>(new Set());
  const [runError, setRunError] = useState("");
  const [columnModal, setColumnModal] = useState<{ open: boolean; editing: EvaluatorDef | null }>({
    open: false,
    editing: null,
  });
  useEffect(() => {
    void listEvaluators().then(setEvaluators).catch(() => {});
    void getEvaluatorCost(30).then((c) => setEvalCost(c.evaluators)).catch(() => {});
  }, []);

  // ── rolling summary: fetch a thread's by-level summaries once (the conversation row triggers
  // it), merged into id-keyed maps so the turn/step cells read by trace_id / span_id. ──
  const [rsum, setRsum] = useState<{
    conversations: Record<string, SummaryItems | null>;
    traces: Record<string, SummaryItems | null>;
    spans: Record<string, SummaryItems | null>;
  }>({ conversations: {}, traces: {}, spans: {} });
  const [rsumGenerating, setRsumGenerating] = useState<Set<string>>(new Set());
  const rsumInflight = useRef<Set<string>>(new Set());
  // ponytail: one shared promise chain — the loads run strictly one at a time. Every conversation
  // row's cell calls `ensure` on mount, so a full page fired one request PER ROW simultaneously;
  // each holds two Postgres connections server-side (the auth dependency for the whole request,
  // plus a sync one while the handler works in the threadpool), which exhausted the connection
  // limit and 500'd every other page rendering at that moment. Swap in a small concurrency
  // limiter if one-at-a-time ever feels slow — this column is lazy decoration, so it doesn't yet.
  const rsumChain = useRef<Promise<void>>(Promise.resolve());

  const loadRsum = useCallback((thread: string) => {
    rsumInflight.current.add(thread);
    rsumChain.current = rsumChain.current.then(() =>
      fetch(`/api/sessions/${encodeURIComponent(thread)}/rolling-summary/by-level`)
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          // A failed load still latches `conversations[thread]` (to null): `ensure`'s guard is
          // `!== undefined`, so leaving it unset re-queued the same thread on every render — one
          // failure became a permanent request storm, which is how the outage above sustained itself.
          setRsum((p) => ({
            conversations: { ...p.conversations, [thread]: d?.conversation ?? null },
            traces: { ...p.traces, ...(d?.traces ?? {}) },
            spans: { ...p.spans, ...(d?.spans ?? {}) },
          }));
        })
        .catch(() => {})
        .finally(() => rsumInflight.current.delete(thread)),
    );
    return rsumChain.current;
  }, []);

  const ensureRsum = useCallback(
    (thread: string) => {
      if (!thread || rsumInflight.current.has(thread) || rsum.conversations[thread] !== undefined) return;
      void loadRsum(thread);
    },
    [loadRsum, rsum.conversations],
  );

  const generateRsum = useCallback(
    (thread: string) => {
      if (!thread || rsumGenerating.has(thread)) return;
      setRsumGenerating((p) => new Set(p).add(thread));
      void fetch(`/api/sessions/${encodeURIComponent(thread)}/rolling-summary/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      })
        .then(() => loadRsum(thread))
        .catch(() => {})
        .finally(() => setRsumGenerating((p) => { const n = new Set(p); n.delete(thread); return n; }));
    },
    [loadRsum, rsumGenerating],
  );

  const rsumView = useMemo<RollingSummaryView>(
    () => ({
      conversations: rsum.conversations,
      traces: rsum.traces,
      spans: rsum.spans,
      ensure: ensureRsum,
      generate: generateRsum,
      generating: rsumGenerating,
    }),
    [rsum, ensureRsum, generateRsum, rsumGenerating],
  );

  const anyState = useMemo(
    () => Object.values(spans).some((list) => Array.isArray(list) && list.some(spanHasState)),
    [spans],
  );

  // Fixed columns first, then EVERY metric column at the right end of the table (ordered
  // C-metrics → M-metrics → S-metrics, creation order within a level), each with its own
  // cycled column tint — the TurnWise layout.
  const allColumns = useMemo<Col[]>(() => {
    const groupOrder: Record<Group, number> = { C: 0, M: 1, S: 2 };
    const metric: Col[] = [...evaluators]
      .sort((a, b) => groupOrder[levelGroup(a.level)] - groupOrder[levelGroup(b.level)])
      .map((ev, i) => ({
        key: `eval:${ev.score_name}`,
        label: ev.name,
        group: levelGroup(ev.level),
        // sized so chip + 16-char teaser pill fit without spilling into the next column
        width: 230,
        evaluator: ev,
        tint: METRIC_TINTS[i % METRIC_TINTS.length],
      }));
    // Drop the State Δ columns entirely when nothing loaded carries state — a project that sends
    // none would otherwise get two permanently-empty columns it never asked for. They appear as
    // soon as a conversation with state is expanded.
    const fixed = anyState ? COLUMNS : COLUMNS.filter((c) => c.key !== "mstate" && c.key !== "sstate");
    return [...fixed, ...metric];
  }, [evaluators, anyState]);
  const cols = useMemo(() => allColumns.filter((c) => !hidden.has(c.key)), [allColumns, hidden]);

  async function runScope(scope: RunScope, busy: { cols?: string[]; rows?: string[] }) {
    setRunError("");
    if (busy.cols?.length) setBusyCols((p) => new Set([...p, ...busy.cols!]));
    if (busy.rows?.length) setBusyRows((p) => new Set([...p, ...busy.rows!]));
    try {
      await streamEvaluationRun(scope, (e) => {
        if (e.type === "result") {
          const key = scoreKey(e.score);
          if (key) liveStore.set(key, e.score);
        } else if (e.type === "target_error") {
          setRunError(`${e.target}: ${e.detail}`);
        } else if (e.type === "error") {
          setRunError(e.detail);
        }
      });
    } catch (err) {
      setRunError(err instanceof Error ? err.message : "evaluation run failed");
    } finally {
      if (busy.cols?.length) setBusyCols((p) => { const n = new Set(p); busy.cols!.forEach((c) => n.delete(c)); return n; });
      if (busy.rows?.length) setBusyRows((p) => { const n = new Set(p); busy.rows!.forEach((r) => n.delete(r)); return n; });
    }
  }

  function threadBusyKeys(thread: string): string[] {
    const t = turns[thread];
    return [`th:${thread}`, ...(Array.isArray(t) ? t.map((x) => `tr:${x.trace_id}`) : [])];
  }

  const evalView = useMemo<EvalView>(() => ({
    busyCols,
    busyRows,
    hasEvaluators: evaluators.length > 0,
    runThread: (thread) => void runScope({ thread_ids: [thread] }, { rows: threadBusyKeys(thread) }),
    runTrace: (trace) => void runScope({ trace_ids: [trace] }, { rows: [`tr:${trace}`] }),
    runColumn: (ev) =>
      void runScope(
        { evaluator_ids: [ev.id], thread_ids: conversations.map((c) => c.thread) },
        { cols: [ev.score_name] },
      ),
    editColumn: (ev) => setColumnModal({ open: true, editing: ev }),
    removeColumn: (ev) => {
      if (!window.confirm(`Delete the "${ev.name}" column? Past results stay in the score history.`)) return;
      void deleteEvaluator(ev.id)
        .then(() => listEvaluators().then(setEvaluators))
        // Say why it failed and what to do about it — the API's own "insufficient role" in a red
        // banner reads as the product being broken.
        .catch((e) => setRunError(deleteColumnError(e, ev.name)));
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [busyCols, busyRows, evaluators, conversations, turns]);

  function runAllEvals() {
    void runScope(
      { thread_ids: conversations.map((c) => c.thread) },
      { cols: evaluators.map((e) => e.score_name) },
    );
  }
  const anyRunning = busyCols.size > 0 || busyRows.size > 0;
  // Types listed in the filter menu: the canonical set (always) plus any extra types the current
  // data emits (so SDK additions show up automatically). Canonical order first, extras alphabetical.
  const spanTypes = useMemo(() => {
    const present = new Set<string>();
    const add = (arr?: SpanOut[]) => arr?.forEach((s) => s.type && present.add(normalizeType(s.type)));
    Object.values(spans).forEach((v) => Array.isArray(v) && add(v));
    conversations.forEach((c) => c.turnsData?.forEach((t) => add(t.spans)));
    const canonical = new Set<string>(KNOWN_SPAN_TYPES);
    const extras = [...present].filter((t) => !canonical.has(t)).sort();
    return [...KNOWN_SPAN_TYPES, ...extras];
  }, [spans, conversations]);

  // Restore saved view prefs on mount, then persist on change (skip writes until loaded so the
  // initial defaults don't clobber what's stored).
  const [prefsLoaded, setPrefsLoaded] = useState(false);
  useEffect(() => {
    try {
      const raw = localStorage.getItem(PREFS_KEY);
      if (raw) {
        const p = JSON.parse(raw) as { hidden?: unknown };
        if (Array.isArray(p.hidden)) setHidden(new Set(p.hidden as string[]));
      }
    } catch {
      /* ignore */
    }
    setPrefsLoaded(true);
  }, []);
  useEffect(() => {
    if (!prefsLoaded) return;
    try {
      // Read-modify-write so we don't clobber the hiddenTypes field owned by useHiddenTypes().
      const raw = localStorage.getItem(PREFS_KEY);
      const cur = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
      localStorage.setItem(PREFS_KEY, JSON.stringify({ ...cur, hidden: [...hidden] }));
    } catch {
      /* ignore */
    }
  }, [prefsLoaded, hidden]);

  function toggleCol(key: string) {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  return (
    <EvalViewContext.Provider value={evalView}>
      <LiveScoreContext.Provider value={liveStore}>
      <RollingSummaryContext.Provider value={rsumView}>
      <SelectContext.Provider value={selectView}>
      <div
        style={!embedded && wide ? WIDE_STYLE : undefined}
        className="overflow-hidden rounded-lg border border-line transition-[width,margin] duration-200"
      >
        <div className="flex items-center justify-between border-b border-line bg-ink-700/50 px-4 py-2">
          <div className="flex items-center gap-1">
            <button onClick={toggleAll} className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs text-fg-muted transition-colors hover:bg-ink-700 hover:text-fg">
              <ChevronsUpDown className="h-3.5 w-3.5" />
              <span>{allOpen ? "Collapse All" : "Expand All"}</span>
            </button>
            {!shared && evaluators.length > 0 && conversations.length > 0 && (
              <button
                onClick={runAllEvals}
                disabled={anyRunning}
                className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs text-fg-muted transition-colors hover:bg-ink-700 hover:text-ok disabled:opacity-50"
                title="Run every evaluation column on all loaded rows"
              >
                {anyRunning ? (
                  <span className="h-3 w-3 animate-spin rounded-full border-2 border-line border-t-ok" />
                ) : (
                  <Play className="h-3.5 w-3.5 text-ok" />
                )}
                <span>Run evals</span>
              </button>
            )}
            {selectView.enabled && selected.size > 0 && (
              <>
                <span className="ml-1 h-5 w-px bg-ink-600" aria-hidden />
                <span className="px-1 font-mono text-[11px] text-fg-faint">{selected.size} selected</span>
                <button
                  onClick={() => void deleteSelected()}
                  disabled={deleting}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-fail/40 bg-fail/10 px-3 py-1.5 text-xs font-medium text-fail transition-colors hover:bg-fail/20 disabled:opacity-50"
                  title="Delete the selected conversations"
                >
                  <Trash className="h-3.5 w-3.5" />
                  <span>{deleting ? "Deleting…" : "Delete"}</span>
                </button>
                <button
                  onClick={() => setSelected(new Set())}
                  className="rounded-lg px-2 py-1.5 text-xs text-fg-faint transition-colors hover:bg-ink-700 hover:text-fg"
                >
                  Clear
                </button>
              </>
            )}
          </div>
          <div className="flex items-center gap-1">
            {!shared && (
              <button
                onClick={() => setColumnModal({ open: true, editing: null })}
                className="inline-flex items-center gap-1.5 rounded-lg border border-signal/40 bg-signal/15 px-3 py-1.5 text-xs font-medium text-signal transition-all hover:bg-signal/25 hover:shadow-glow"
                title="Add an evaluation column"
              >
                <PlusIcon className="h-3.5 w-3.5" />
                <span>Add Column</span>
              </button>
            )}
            {!embedded && <WideToggle wide={wide} onToggle={() => setWide(!wide)} />}
            <div className="relative">
              <button onClick={() => setTypeMenu((o) => !o)} className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs text-fg-muted transition-colors hover:bg-ink-700 hover:text-fg" title="Filter step types">
                <FilterIcon className="h-3.5 w-3.5" />
                <span>Types</span>
                {hiddenTypes.size > 0 && <span className="rounded bg-signal/20 px-1.5 text-[10px] font-medium text-signal">{hiddenTypes.size}</span>}
              </button>
              {typeMenu && (
                <TypesMenu types={spanTypes} hidden={hiddenTypes} source={typesSource}
                  onToggle={toggleType} onReset={resetTypes}
                  onUseDefault={useWorkspaceDefault} onSaveDefault={saveAsWorkspaceDefault}
                  onClose={() => setTypeMenu(false)} />
              )}
            </div>
            <div className="relative">
              <button onClick={() => setColMenu((o) => !o)} className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs text-fg-muted transition-colors hover:bg-ink-700 hover:text-fg" title="Manage Column Visibility">
                <Eye className="h-3.5 w-3.5" />
                <span>Columns</span>
              </button>
              {colMenu && <ColumnsMenu all={allColumns} hidden={hidden} cost={evalCost} onToggle={toggleCol} onClose={() => setColMenu(false)} />}
            </div>
          </div>
        </div>

        {runError && (
          <div className="flex items-center justify-between gap-3 border-b border-fail/20 bg-fail/[0.06] px-4 py-2 text-[12px] text-fail">
            <span className="truncate">{runError}</span>
            <button onClick={() => setRunError("")} className="shrink-0 rounded px-1.5 text-fail hover:bg-fail/10" aria-label="Dismiss">
              ✕
            </button>
          </div>
        )}

        <div className="overflow-x-auto">
          <table className="w-full min-w-[800px] border-collapse">
            <thead>
              <HeaderRow cols={cols} sort={sort} />
            </thead>
            <tbody>
              {conversations.length === 0 ? (
                <tr>
                  {/* CTRL_CELLS, not a hardcoded count — this row is outside the SelectContext
                      provider below, so it cannot call useCtrlCount(), but it must agree with it. */}
                  <td colSpan={CTRL_CELLS + (selectView.enabled ? 1 : 0) + cols.length} className="px-6 py-14 text-center text-sm text-fg-faint">
                    No conversations.
                  </td>
                </tr>
              ) : (
                conversations.map((c) => (
                  <ConvRows
                    key={c.thread}
                    conv={c}
                    turns={turns[c.thread]}
                    spansCache={spans}
                    open={openConv.has(c.thread)}
                    openTurn={openTurn}
                    cols={cols}
                    hiddenTypes={hiddenTypes}
                    onToggleConv={toggleConv}
                    onToggleTurn={toggleTurn}
                    onShowAgents={setAgentsThread}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      <AddColumnModal
        open={!shared && columnModal.open}
        editing={columnModal.editing}
        previewThread={[...openConv][0] ?? conversations[0]?.thread}
        onClose={() => setColumnModal({ open: false, editing: null })}
        onSaved={() => void listEvaluators().then(setEvaluators)}
      />
      {agentsThread && <AgentsSidePanel threadId={agentsThread} onClose={() => setAgentsThread(null)} />}
      </SelectContext.Provider>
      </RollingSummaryContext.Provider>
      </LiveScoreContext.Provider>
    </EvalViewContext.Provider>
  );
}
