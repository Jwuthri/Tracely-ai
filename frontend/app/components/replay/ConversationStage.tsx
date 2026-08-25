"use client";

import clsx from "clsx";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  activeAt, currentByActor, findSpan, fmtMs, isContainer, kindStyle, onStage, orderActors,
  payloadText, realMsAt, toPlayEvents,
  type PlayEvent, type ReplayActor, type ReplayEvent, type SpanDetail,
} from "./timeline";
import { useHiddenTypes } from "../../lib/typePrefs";

/* The conversation, acted out: one character per agent, sub-agents pulled in under the agent
   that spawned them, every tool/llm call a beat on a shared clock you can scrub. */

const SPEEDS = [0.5, 1, 2, 4];

export function ConversationStage({ threadId }: { threadId: string }) {
  const [data, setData] = useState<{ actors: ReplayActor[]; events: ReplayEvent[]; durationMs: number } | null>(null);
  const [failed, setFailed] = useState(false);
  const [t, setT] = useState(0);
  const [playing, setPlaying] = useState(true);
  const [speed, setSpeed] = useState(1);
  const raf = useRef<number | null>(null);
  const last = useRef<number>(0);

  useEffect(() => {
    let alive = true;
    fetch(`/api/session-replay?thread=${encodeURIComponent(threadId)}`, { cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error(String(r.status));
        return r.json();
      })
      .then((d) => alive && setData({ actors: d.actors ?? [], events: d.events ?? [], durationMs: d.duration_ms ?? 0 }))
      .catch(() => {
        // a backend failure is NOT an empty conversation — saying "nothing to replay" when the
        // endpoint 500s sends people looking for a bug in their instrumentation
        if (alive) {
          setFailed(true);
          setData({ actors: [], events: [], durationMs: 0 });
        }
      });
    return () => { alive = false; };
  }, [threadId]);

  // Same "filter step types" preference as the table/timeline: hidden types never enter the
  // replay. Asks are synthetic (type "") and can't be hidden.
  const { hidden: hiddenTypes, reset: resetTypes } = useHiddenTypes();
  const { events, total } = useMemo(
    () => toPlayEvents((data?.events ?? []).filter((e) => !e.type || !hiddenTypes.has(e.type))),
    [data, hiddenTypes],
  );
  const actors = useMemo(() => orderActors(data?.actors ?? []), [data]);
  const durationMs = data?.durationMs ?? 0;

  useEffect(() => {
    if (!playing || !total) return;
    // The clock advances every frame but only COMMITS at ~25fps: a React commit here re-renders
    // every step row and lane block, and 60fps of that is pure waste — the stage reads identically.
    const COMMIT_MS = 40;
    let pending = 0;
    const tick = (now: number) => {
      const dt = last.current ? now - last.current : 16;
      last.current = now;
      pending += dt * speed;
      if (pending >= COMMIT_MS) {
        const step = pending;
        pending = 0;
        setT((prev) => {
          const next = prev + step;
          if (next >= total) { setPlaying(false); return total; }
          return next;
        });
      }
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => {
      if (raf.current) cancelAnimationFrame(raf.current);
      last.current = 0;
    };
  }, [playing, speed, total]);

  const live = activeAt(events, t);
  const logRef = useRef<HTMLDivElement>(null);
  const [selected, setSelected] = useState<PlayEvent | null>(null);
  const [spans, setSpans] = useState<Record<string, SpanDetail[]>>({});

  // The step's real input/output lives on the trace, not in the replay script (which stays small).
  // Fetched once per trace and cached, so clicking around a conversation costs one request a turn.
  useEffect(() => {
    const trace = selected?.trace_id;
    if (!trace || spans[trace]) return;
    let alive = true;
    fetch(`/api/trace?id=${encodeURIComponent(trace)}`, { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => alive && setSpans((prev) => ({ ...prev, [trace]: d.spans ?? [] })))
      .catch(() => alive && setSpans((prev) => ({ ...prev, [trace]: [] })));
    return () => { alive = false; };
  }, [selected, spans]);

  const pick = (e: PlayEvent) => { setSelected(e); setT(e.pt); setPlaying(false); };
  const liveKey = live.length ? `${live[0].trace_id}:${live[0].span_id}` : "";
  useEffect(() => {
    if (!liveKey) return;
    logRef.current?.querySelector(`[data-step="${CSS.escape(liveKey)}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [liveKey]);
  const current = currentByActor(events, t);
  const liveIds = new Set(live.map((e) => e.span_id));
  const played = events.filter((e) => e.pt <= t);

  if (!data) return <div className="card grid h-[420px] place-items-center font-mono text-[12px] text-fg-muted">loading the conversation…</div>;
  if (!events.length) {
    return (
      <div className="card grid h-[320px] place-items-center text-center">
        <div>
          <p className="text-[15px] font-semibold">
            {failed ? "Couldn't load the conversation" : "Nothing to replay"}
          </p>
          <p className="mt-1 text-[13px] text-fg-muted">
            {failed
              ? "The replay endpoint answered with an error — try reloading."
              : "This conversation has no spans yet."}
          </p>
        </div>
      </div>
    );
  }

  const restart = () => { setT(0); setPlaying(true); setSelected(null); };

  return (
    <div className="space-y-4">
      {/* ── controls ── */}
      <div className="flex flex-wrap items-center gap-3">
        <button onClick={() => (t >= total ? restart() : setPlaying((p) => !p))} className="btn-primary !py-1.5">
          {t >= total ? "↺ replay" : playing ? "❚❚ pause" : "▶ play"}
        </button>
        <button onClick={restart} className="btn-ghost">↺ restart</button>
        <div className="flex gap-1">
          {SPEEDS.map((s) => (
            <button key={s} onClick={() => setSpeed(s)}
              className={clsx("rounded-md border px-2 py-1 font-mono text-[11px] transition-colors",
                speed === s ? "border-signal/40 bg-signal/15 text-signal" : "border-line text-fg-muted hover:text-fg")}>
              {s}×
            </button>
          ))}
        </div>
        {hiddenTypes.size > 0 && (
          <button onClick={resetTypes}
            title={`Step types hidden by the table/timeline filter: ${[...hiddenTypes].join(", ")} — click to show all`}
            className="rounded-md border border-warn/30 bg-warn/10 px-2 py-1 font-mono text-[10px] text-warn hover:brightness-125">
            {hiddenTypes.size} type{hiddenTypes.size > 1 ? "s" : ""} hidden ✕
          </button>
        )}
        <span className="ml-auto font-mono text-[11px] text-fg-faint"
          title="Real trace time — the play clock squeezes pauses and long calls">
          {fmtMs(realMsAt(events, t))} / {fmtMs(durationMs)} · {played.length}/{events.length} steps
        </span>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1fr_320px]">
        {/* ── stage ── */}
        <div className="card relative overflow-hidden p-5">
          <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_50%_0%,rgba(34,211,238,0.06),transparent_60%)]" />
          <div className="relative space-y-3">
            {actors.map((a) => {
              const here = onStage(a, events, t);
              const doing = current[a.id];
              const busy = live.some((e) => e.actor === a.id && !isContainer(e));
              const failed = played.some((e) => e.actor === a.id && e.status === "error");
              return (
                <div key={a.id}
                  className={clsx("flex items-center gap-3 rounded-xl border p-3 transition-all duration-500",
                    a.depth > 0 && "ml-10",
                    here ? "border-line-bright bg-ink-800/80 opacity-100" : "border-line-soft bg-ink-900/40 opacity-35",
                    busy && "shadow-glow",
                    failed && here && "border-fail/50")}>
                  <Character actor={a} busy={busy} here={here} failed={failed} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-[14px] font-semibold">{a.name}</span>
                      {a.kind === "subagent" && (
                        <span className="rounded border border-t_think/40 bg-t_think/10 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider text-t_think">
                          sub-agent
                        </span>
                      )}
                      {a.kind === "customer" && (
                        <span className="rounded border border-warn/40 bg-warn/10 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider text-warn">
                          customer
                        </span>
                      )}
                      {busy && <span className="font-mono text-[10px] text-ok">● working</span>}
                    </div>
                    <div className="mt-1 h-[22px]">
                      {here && doing ? (
                        <span key={doing.span_id}
                          className={clsx("stage-pop inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 font-mono text-[11px]",
                            doing.status === "error" ? "border-fail/50 bg-fail-dim/50 text-fail" : "border-line bg-ink-700 text-fg")}
                          style={doing.status === "ok" ? { borderColor: `${kindStyle(doing.kind).color}55` } : undefined}>
                          <span style={{ color: kindStyle(doing.kind).color }}>{kindStyle(doing.kind).icon}</span>
                          {/* the customer's beat IS the question — the span name says nothing */}
                          <span className="max-w-[420px] truncate">{doing.kind === "ask" ? doing.detail || "…" : doing.name}</span>
                          {/* a model turn that answered with a tool call: name the tool, or the
                              row reads as an anonymous model call doing nothing */}
                          {doing.kind === "llm" && doing.calls?.length ? (
                            <span className="text-t_tool">→ {doing.calls.join(", ")}</span>
                          ) : null}
                          {doing.model && <span className="text-fg-faint">· {doing.model}</span>}
                          {liveIds.has(doing.span_id) && <span className="stage-dots" />}
                        </span>
                      ) : (
                        <span className="font-mono text-[11px] text-fg-faint">{here ? "…" : "not in this conversation yet"}</span>
                      )}
                    </div>
                  </div>
                  <div className="shrink-0 text-right font-mono text-[10px] text-fg-faint">
                    <div>{a.events} steps</div>
                    {a.errors > 0 && <div className="text-fail">{a.errors} failed</div>}
                  </div>
                </div>
              );
            })}
          </div>
          {selected && (
            <StepDetail
              event={selected}
              actor={actors.find((a) => a.id === selected.actor)}
              span={findSpan(spans[selected.trace_id], selected.span_id)}
              loading={!spans[selected.trace_id]}
              onClose={() => setSelected(null)}
            />
          )}
        </div>

        {/* ── step log, synced to the playhead ── */}
        <aside className="card flex max-h-[520px] flex-col overflow-hidden">
          <div className="border-b border-line px-4 py-2.5 font-mono text-[11px] uppercase tracking-wider text-fg-muted">
            steps
          </div>
          <div ref={logRef} className="flex-1 space-y-0.5 overflow-y-auto p-2">
            {events.map((e) => {
              const done = e.pt <= t;
              const isLive = liveIds.has(e.span_id);
              const st = kindStyle(e.kind);
              return (
                <button key={`${e.trace_id}:${e.span_id}`}
                  data-step={`${e.trace_id}:${e.span_id}`}
                  onClick={() => pick(e)}
                  className={clsx("flex w-full items-center gap-2 rounded-md px-2 py-1 text-left transition-all",
                    done ? "opacity-100" : "opacity-30",
                    selected?.span_id === e.span_id && selected?.trace_id === e.trace_id
                      ? "bg-signal/20 ring-1 ring-signal/60"
                      : isLive && "bg-signal/10 ring-1 ring-signal/30",
                    e.status === "error" && done && "bg-fail-dim/40")}>
                  <span className="w-[34px] shrink-0 text-right font-mono text-[9px] text-fg-faint">{fmtMs(e.t_ms)}</span>
                  <span style={{ color: st.color }} className="shrink-0 text-[11px]">{st.icon}</span>
                  <span className="min-w-0 flex-1 truncate font-mono text-[11px]">{e.name}</span>
                  {e.status === "error" && <span className="font-mono text-[9px] text-fail">FAIL</span>}
                </button>
              );
            })}
          </div>
        </aside>
      </div>

      {/* ── lanes + scrubber ── */}
      <Lanes actors={actors} events={events} total={total} t={t}
        onSeek={(v) => { setT(v); setPlaying(false); }} onPick={pick} selected={selected} />

      <p className="text-[12px] text-fg-muted">
        Long pauses between turns are squeezed so the replay stays watchable — the timestamps in the
        step list are the real ones. <Link href={`/sessions/${encodeURIComponent(threadId)}`} className="text-signal hover:underline">Open the full conversation →</Link>
      </p>
    </div>
  );
}

function Lanes({ actors, events, total, t, onSeek, onPick, selected }: {
  actors: ReplayActor[]; events: PlayEvent[]; total: number; t: number; onSeek: (v: number) => void;
  onPick: (e: PlayEvent) => void; selected: PlayEvent | null;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const seek = (clientX: number) => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    onSeek(Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * total);
  };
  return (
    <div className="card p-4">
      <div className="space-y-1.5">
        {actors.map((a) => (
          <div key={a.id} className="flex items-center gap-3">
            <span className={clsx("w-[120px] shrink-0 truncate font-mono text-[10px]", a.depth ? "pl-3 text-fg-faint" : "text-fg-muted")}
              title={a.name}>
              {a.depth ? "└ " : ""}{a.name}
            </span>
            <div className="relative h-5 flex-1 overflow-hidden rounded bg-ink-800">
              {events.filter((e) => e.actor === a.id).map((e) => {
                const st = kindStyle(e.kind);
                const wrap = isContainer(e);
                return (
                  <span key={`${e.trace_id}:${e.span_id}`}
                    onClick={() => onSeek(e.pt)}
                    title={wrap ? `${e.name} · wraps ${fmtMs(e.dur_ms)} of the turn` : `${e.name} · ${fmtMs(e.dur_ms)}`}
                    className={clsx("absolute cursor-pointer transition-opacity hover:opacity-100",
                      wrap ? "top-0 h-5 rounded-[3px] border border-dashed" : "top-1 h-3 rounded-[2px]")}
                    style={{
                      left: `${(e.pt / total) * 100}%`,
                      width: `${Math.max(0.6, (e.pdur / total) * 100)}%`,
                      ...(wrap
                        ? { borderColor: `${st.color}66`, background: `${st.color}0f` }
                        : { background: e.status === "error" ? "#fb7185" : st.color }),
                      opacity: e.pt <= t ? 0.95 : 0.28,
                    }} />
                );
              })}
            </div>
          </div>
        ))}
      </div>
      <div ref={ref} onMouseDown={(e) => seek(e.clientX)} className="relative mt-3 ml-[132px] h-6 cursor-pointer">
        <div className="absolute inset-x-0 top-2.5 h-1 rounded bg-ink-700" />
        <div className="absolute top-2.5 h-1 rounded bg-signal/60" style={{ width: `${(t / total) * 100}%` }} />
        <div className="absolute top-0 h-6 w-[2px] bg-signal shadow-glow" style={{ left: `${(t / total) * 100}%` }} />
      </div>
    </div>
  );
}

/** A tiny pixel character; hue is derived from the actor id so it's stable per agent. */
function Character({ actor, busy, here, failed }: { actor: ReplayActor; busy: boolean; here: boolean; failed: boolean }) {
  let h = 0;
  for (let i = 0; i < actor.id.length; i++) h = (h * 31 + actor.id.charCodeAt(i)) >>> 0;
  const hue = h % 360;
  const size = actor.depth ? 34 : 42;
  return (
    <div className={clsx("relative shrink-0 transition-transform duration-300", busy && "stage-bob")}
      style={{ width: size, height: size }}>
      <svg viewBox="0 0 36 39" width={size} height={size} className={clsx(!here && "grayscale")}>
        <rect x="9" y="0" width="18" height="9" rx="3" fill={`hsl(${(hue + 140) % 360} 45% 30%)`} />
        <rect x="9" y="7" width="18" height="12" rx="2" fill="hsl(28 55% 74%)" />
        <rect x="13" y="11" width="3" height="3" fill="#1a2030" />
        <rect x="21" y="11" width="3" height="3" fill="#1a2030" />
        <rect x="15" y="16" width="7" height="2" rx="1" fill="#1a2030" opacity="0.6" />
        <rect x="6" y="19" width="24" height="14" rx="4" fill={`hsl(${hue} 62% 52%)`} />
        <rect x="12" y="33" width="5" height="6" fill="#222a3a" />
        <rect x="20" y="33" width="5" height="6" fill="#222a3a" />
      </svg>
      {busy && <span className="absolute -right-1 -top-1 h-3 w-3 animate-ping rounded-full bg-ok/70" />}
      {failed && <span className="absolute -bottom-1 -right-1 grid h-4 w-4 place-items-center rounded-full bg-fail text-[9px] font-bold text-ink-900">!</span>}
    </div>
  );
}

/** What actually happened in the selected step: the real timestamp, the model, and the span's
 *  input/output pulled from its trace. Shown on the stage so the character and the detail are
 *  read together rather than in two places. */
function StepDetail({ event, actor, span, loading, onClose }: {
  event: PlayEvent; actor?: ReplayActor; span: SpanDetail | null; loading: boolean; onClose: () => void;
}) {
  const st = kindStyle(event.kind);
  const input = payloadText(span?.input);
  const output = payloadText(span?.output);
  return (
    <div className="stage-pop mt-4 rounded-xl border border-line-bright bg-ink-900/80 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span style={{ color: st.color }} className="text-[13px]">{st.icon}</span>
        <span className="font-mono text-[13px] font-semibold">{event.name}</span>
        <span className="rounded border border-line px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider text-fg-muted">
          {st.label}
        </span>
        {event.status === "error" && (
          <span className="rounded border border-fail/50 bg-fail-dim/50 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider text-fail">
            failed
          </span>
        )}
        <button onClick={onClose} className="ml-auto font-mono text-[11px] text-fg-faint hover:text-fg">✕ close</button>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[10.5px] text-fg-faint">
        <span>by <span className="text-fg-muted">{actor?.name ?? event.actor}</span></span>
        <span>at {fmtMs(event.t_ms)}</span>
        <span>took {fmtMs(span?.latency_ms ?? event.dur_ms)}</span>
        {event.model && <span>{event.model}</span>}
        {span?.tokens ? <span>{span.tokens.toLocaleString("en-US")} tokens</span> : null}
      </div>

      {event.status === "error" && (span?.status_message || event.detail) && (
        <p className="mt-2 rounded-md border border-fail/30 bg-fail-dim/30 px-2.5 py-1.5 font-mono text-[11px] text-fail">
          {span?.status_message || event.detail}
        </p>
      )}

      {loading ? (
        <p className="mt-3 font-mono text-[11px] text-fg-faint">loading step data…</p>
      ) : input || output ? (
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          {[["input", input], ["output", output]].map(([label, body]) =>
            body ? (
              <div key={label}>
                <div className="mb-1 font-mono text-[9px] uppercase tracking-wider text-fg-faint">{label}</div>
                <pre className="max-h-[160px] overflow-auto rounded-md border border-line bg-ink-950/60 p-2.5 font-mono text-[11px] leading-relaxed text-fg-muted">
                  {body}
                </pre>
              </div>
            ) : null,
          )}
        </div>
      ) : (
        <p className="mt-3 font-mono text-[11px] text-fg-faint">This span recorded no input or output.</p>
      )}

      <Link href={`/traces/${encodeURIComponent(event.trace_id)}`}
        className="mt-3 inline-block font-mono text-[11px] text-signal hover:underline">
        open this turn's full trace →
      </Link>
    </div>
  );
}
