"use client";

import clsx from "clsx";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { alarmAt, breakPlan, layoutOffice, librarySkills, narrate, phoneStateAt, poseAt, stationInfo, turnDigest, wallTools, type Bubble, type DeclaredTool, type Pose, type StationInfo, type TurnDigest } from "./office";
import { useOfficeSounds, verdictJingle } from "./sounds";
import { Bookshelf, BreakCouch, CoffeeMachine, Desk, OfficeDoor, PingPongTable, PixelPerson, Plant, ReceptionCounter, ToolsRack, WallClock, WallPoster, WallWindow, WaterCooler } from "./sprites";
import { fmtMs, isContainer, isCustomer, OFFICE_PACING, orderActors, realMsAt, toPlayEvents, type PlayEvent, type ReplayActor, type ReplayEvent } from "./timeline";
import { usePlayClock, useWalking } from "./useClock";
import { useHiddenTypes } from "../../lib/typePrefs";

/* The Fleet office: the conversation acted out as a scene. Every character is a real agent
   from the trace. The customer asks at the reception counter and the supervisor takes the
   question there; a delegation is a phone call ☎ that summons the sub-agent from the break
   room (coffee + pong, bottom-left) to their desk. Long station beats walk over, GRAB the
   tool/skill and run it back at the desk; thinking is a thought cloud; everyone's last word
   stays over their head until they act again. */

type Declared = { name: string; description: string; tools: DeclaredTool[] };
type Payload = { actors: ReplayActor[]; events: ReplayEvent[]; declared: Declared[]; durationMs: number };

const SPEEDS = [0.5, 1, 2, 4];

const hueOf = (id: string) => {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return h % 360;
};

export function OfficeStage({ threadId, src, verdict = null, shared = false }: {
  threadId: string;
  /** Replay payload URL override — the public share page passes its token-scoped endpoint. */
  src?: string;
  /** The conversation's eval verdict, for the end-of-recording finale. */
  verdict?: "PASS" | "FAIL" | null;
  /** Public share view: no links back into the authed app. */
  shared?: boolean;
}) {
  const [data, setData] = useState<Payload | null>(null);
  const [failed, setFailed] = useState(false);
  const [follow, setFollow] = useState(false);
  const [sound, setSound] = useState(false);
  // one selection for the whole office: a person at a desk, a book on the shelf, a tool on
  // the wall — they all open the same side panel.
  const [selected, setSelected] = useState<{ k: "agent" | "skill" | "tool"; id: string } | null>(null);
  const pick = (k: "agent" | "skill" | "tool", id: string) =>
    setSelected((cur) => (cur?.k === k && cur.id === id ? null : { k, id }));

  useEffect(() => {
    let alive = true;
    fetch(src ?? `/api/session-replay?thread=${encodeURIComponent(threadId)}`, { cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error(String(r.status));
        return r.json();
      })
      .then((d) => alive && setData({ actors: d.actors ?? [], events: d.events ?? [], declared: d.declared ?? [], durationMs: d.duration_ms ?? 0 }))
      .catch(() => {
        // a backend failure is NOT an empty office — say so instead of lying
        if (alive) {
          setFailed(true);
          setData({ actors: [], events: [], declared: [], durationMs: 0 });
        }
      });
    return () => { alive = false; };
  }, [threadId, src]);

  // The shared "filter step types" preference (table/timeline) applies here too: a hidden
  // span type never reaches the office. Asks are synthetic (type "") and can't be hidden.
  const { hidden: hiddenTypes, reset: resetTypes } = useHiddenTypes();
  const { events, total } = useMemo(
    () => toPlayEvents(
      (data?.events ?? []).filter((e) => !e.type || !hiddenTypes.has(e.type)),
      OFFICE_PACING,
    ),
    [data, hiddenTypes],
  );
  const actors = useMemo(() => orderActors(data?.actors ?? []), [data]);
  const durationMs = data?.durationMs ?? 0;
  const layout = useMemo(() => layoutOffice(actors), [actors]);
  const skills = useMemo(() => librarySkills(events), [events]);
  const declaredTools = useMemo(
    () => (data?.declared ?? []).flatMap((d) => d.tools),
    [data],
  );
  const tools = useMemo(() => wallTools(events, declaredTools), [events, declaredTools]);
  const staff = useMemo(() => actors.filter((a) => !isCustomer(a)), [actors]);
  const customer = actors.find(isCustomer);
  const turns = useMemo(() => turnDigest(events, actors), [events, actors]);

  const clock = usePlayClock(total, 0.5); // half rate: office scenes read better slow
  const { t } = clock;

  const nameOf = useMemo(() => {
    const m = new Map(actors.map((a) => [a.id, a.name]));
    return (id: string) => m.get(id) ?? id;
  }, [actors]);

  const breaks = useMemo(() => breakPlan(actors, events, t, layout), [actors, events, t, layout]);
  const poses = useMemo(() => {
    const out = new Map<string, Pose>();
    actors.forEach((a, i) => out.set(a.id, poseAt(a, events, t, layout, i, breaks.get(a.id) ?? null)));
    return out;
  }, [actors, events, t, layout, breaks]);
  const pongOn = useMemo(
    () => [...breaks.values()].filter((b) => b.kind.startsWith("pong")).length >= 2,
    [breaks],
  );

  const activeSkill = [...poses.values()].find((p) => p.at === "library")?.action?.name ?? "";
  const activeTool = [...poses.values()].find((p) => p.at === "tools")?.action?.name ?? "";
  const done = total > 0 && t >= total;
  const failures = staff.reduce((n, a) => n + a.errors, 0);
  const doneSign = failures > 0
    ? `end of recording — ${failures} step${failures > 1 ? "s" : ""} failed`
    : "end of recording — everyone did their bit";
  // a hot failure takes over the whole room: red sign, alarm strobe, glitched desk, sweat
  const alarm = alarmAt(events, t);
  const sign = alarm && !done
    ? `⚠ ${nameOf(alarm.actor)}: ${alarm.name} failed`
    : narrate(events, t, nameOf);

  // spend so far on the play clock. LLM spans carry the usage; when only the containers do
  // (some frameworks roll usage up to the root), count those instead — never both.
  const spendOnLeaves = useMemo(
    () => events.some((e) => !isContainer(e) && (e.tokens ?? 0) > 0),
    [events],
  );
  const spend = useMemo(() => {
    let tok = 0;
    let usd = 0;
    for (const e of events) {
      if (e.pt + e.pdur > t || isContainer(e) !== !spendOnLeaves) continue;
      tok += e.tokens ?? 0;
      usd += e.cost ?? 0;
    }
    return { tok, usd };
  }, [events, t, spendOnLeaves]);

  // 🎥 follow: the camera tracks whoever the sign is narrating
  const focus = useMemo(() => {
    if (!follow) return null;
    let cur: PlayEvent | null = null;
    let last: PlayEvent | null = null;
    for (const e of events) {
      if (e.pt > t) break;
      if (isContainer(e) && !e.delegate_to) continue;
      if (t < e.pt + e.pdur) cur = e;
      else last = e;
    }
    const p = poses.get((cur ?? last)?.actor ?? "");
    return p ? { x: p.x, y: p.y } : null;
  }, [follow, events, t, poses]);
  const CAM_SCALE = 1.7;
  const camStyle = focus
    ? (() => {
        const lim = 50 - 50 / CAM_SCALE;
        const cx = Math.max(-lim, Math.min(lim, 50 - focus.x));
        const cy = Math.max(-lim, Math.min(lim, 50 - focus.y));
        return { transform: `scale(${CAM_SCALE}) translate(${cx}%, ${cy}%)` };
      })()
    : undefined;

  useOfficeSounds(events, t, sound);
  const jingled = useRef(false);
  useEffect(() => {
    if (!done || !verdict) { jingled.current = false; return; }
    if (sound && !jingled.current) verdictJingle(verdict);
    jingled.current = true;
  }, [done, verdict, sound]);

  if (!data) {
    return <div className="card grid h-[480px] place-items-center font-mono text-[12px] text-fg-muted">opening the office…</div>;
  }
  if (!events.length) {
    return (
      <div className="card grid h-[320px] place-items-center text-center">
        <div>
          <p className="text-[15px] font-semibold">{failed ? "Couldn't load the conversation" : "The office is empty"}</p>
          <p className="mt-1 text-[13px] text-fg-muted">
            {failed ? "The replay endpoint answered with an error — try reloading." : "This conversation has no spans to act out."}
          </p>
        </div>
      </div>
    );
  }

  const sel = selected?.k === "agent" ? actors.find((a) => a.id === selected.id) : undefined;
  const thing = selected && selected.k !== "agent"
    ? stationInfo(selected.id, selected.k, events, declaredTools)
    : null;

  return (
    <div className="space-y-4">
      {/* ── controls ── */}
      <div className="flex flex-wrap items-center gap-3">
        <button onClick={clock.toggle} className="btn-primary !py-1.5">
          {done ? "↺ replay" : clock.playing ? "❚❚ pause" : "▶ play"}
        </button>
        <button onClick={clock.restart} className="btn-ghost">↺ restart</button>
        <div className="flex gap-1">
          {SPEEDS.map((s) => (
            <button key={s} onClick={() => clock.setSpeed(s)}
              className={clsx("rounded-md border px-2 py-1 font-mono text-[11px] transition-colors",
                clock.speed === s ? "border-signal/40 bg-signal/15 text-signal" : "border-line text-fg-muted hover:text-fg")}>
              {s}×
            </button>
          ))}
        </div>
        <div className="flex gap-1">
          <button onClick={() => setFollow((f) => !f)}
            title="Follow the action with the camera"
            className={clsx("rounded-md border px-2 py-1 font-mono text-[11px] transition-colors",
              follow ? "border-signal/40 bg-signal/15 text-signal" : "border-line text-fg-muted hover:text-fg")}>
            🎥 follow
          </button>
          <button onClick={() => setSound((s) => !s)}
            title="Chiptune office sounds"
            className={clsx("rounded-md border px-2 py-1 font-mono text-[11px] transition-colors",
              sound ? "border-signal/40 bg-signal/15 text-signal" : "border-line text-fg-muted hover:text-fg")}>
            {sound ? "🔊" : "🔇"} sound
          </button>
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
          {fmtMs(realMsAt(events, t))} / {fmtMs(durationMs)}
        </span>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1fr_290px]">
        {/* ── the office ── */}
        {/* The office is a lit room, not a surface: it stays dark in both themes (the pixel
            sprites are painted for it), so pin the palette here — the labels and chips inside
            are tokens and would otherwise flip to ink-on-ink over the floor. */}
        <div data-theme="dark" className="fleet-stage relative aspect-[16/10] select-none overflow-hidden rounded-2xl border border-line shadow-panel">
          {/* the WORLD: everything the 🎥 follow camera pans and zooms lives inside this one
              wrapper; the CRT/vignette/finale overlays stay glued to the frame outside it */}
          <div className="absolute inset-0 transition-transform duration-[900ms] ease-in-out" style={camStyle}>
          {/* wall */}
          <div className="absolute inset-x-0 top-0 h-[17%] border-b-4 border-[#181022] bg-gradient-to-b from-[#2d2542] to-[#251d36]">
            {/* wainscot line */}
            <div className="absolute inset-x-0 bottom-0 h-[10%] bg-[#221a33]" />
            {/* night windows */}
            <div className="absolute left-[5%] top-[12%] w-[8%]"><WallWindow /></div>
            <div className="absolute left-[16%] top-[12%] w-[8%]"><WallWindow moon /></div>
            {/* a framed chart + the office clock */}
            <div className="absolute left-[73.5%] top-[22%] w-[3.2%]"><WallPoster /></div>
            <div className="absolute left-[78.5%] top-[16%] w-[2.6%]"><WallClock /></div>
            {/* LED sign — goes red while a failure is hot */}
            <div className={clsx("fleet-sign absolute left-1/2 top-1/2 w-[44%] -translate-x-1/2 -translate-y-1/2 rounded border border-[#181022] bg-[#0a0f14] px-3 py-1.5",
              alarm && !done && "!border-fail/40")}>
              <p className={clsx("truncate text-center font-mono text-[10px] tracking-wider",
                alarm && !done ? "text-fail" : "text-[#57e39a]")} title={sign}>
                {done ? doneSign : sign}
              </p>
            </div>
            {/* door */}
            <div className="absolute right-[4%] top-[8%] w-[4.5%]"><OfficeDoor /></div>
          </div>
          {/* spend ticker: tokens + cost land as beats complete */}
          {spend.tok > 0 && (
            <div className="absolute right-[9.5%] top-[11%] rounded border border-[#181022] bg-[#0a0f14]/90 px-1.5 py-0.5 font-mono text-[8.5px] tracking-wider text-warn"
              title="Tokens and cost spent so far (completed steps)">
              {spend.tok.toLocaleString()} tok{spend.usd > 0 ? ` · $${spend.usd.toFixed(4)}` : ""}
            </div>
          )}

          {/* floor */}
          <div className="fleet-floor absolute inset-x-0 bottom-0 top-[17%]" />
          {/* window light spilling onto the floor */}
          <div className="fleet-lightpool pointer-events-none absolute left-[3.5%] top-[17%] h-[26%] w-[12.5%]" />
          <div className="fleet-lightpool pointer-events-none absolute left-[16%] top-[17%] h-[26%] w-[12.5%]" />
          {/* rug under the main desk row */}
          <div className="fleet-rug pointer-events-none absolute left-1/2 top-[36%] h-[24%] w-[52%] -translate-x-1/2 rounded-lg" />
          {/* dust motes drifting through the light */}
          <span className="fleet-mote left-[9%] top-[38%]" />
          <span className="fleet-mote left-[21%] top-[33%]" style={{ animationDelay: "2.1s" }} />
          <span className="fleet-mote left-[56%] top-[52%]" style={{ animationDelay: "4.4s" }} />
          <span className="fleet-mote left-[78%] top-[44%]" style={{ animationDelay: "1.2s" }} />
          <span className="fleet-mote left-[40%] top-[70%]" style={{ animationDelay: "5.6s" }} />

          {/* break room: bottom-left corner, its own floor + coffee machine + pong table.
              Idle sub-agents wander in; the pong ball rallies only while two of them play. */}
          <div className="fleet-breakroom pointer-events-none absolute bottom-0 left-0 h-[38%] w-[27%]" />
          <span className="pointer-events-none absolute bottom-[34%] left-[2%] font-mono text-[8px] uppercase tracking-[0.25em] text-fg-faint">
            break room
          </span>
          <div className="absolute bottom-[26%] left-[1.5%] w-[5.5%]"><CoffeeMachine /></div>
          <div className="pointer-events-none absolute bottom-[4%] left-[6%] w-[15%]"><PingPongTable playing={pongOn} /></div>
          <div className="pointer-events-none absolute bottom-[25%] left-[21%] w-[4%]"><WaterCooler /></div>
          <div className="pointer-events-none absolute bottom-[10%] left-[0.5%] w-[6.5%]"><BreakCouch /></div>
          <div className="absolute bottom-[3%] left-[23%] w-[3%]"><Plant /></div>

          {/* reception counter: the customer waits on its right, the supervisor greets from the left */}
          <div className="pointer-events-none absolute left-[81.5%] top-[22%] w-[3%]"><ReceptionCounter /></div>

          {/* fixed furniture */}
          <div className="absolute left-[2%] top-[26%] w-[10%]">
            <Bookshelf skills={skills} active={activeSkill} onPick={(n) => pick("skill", n)} />
          </div>
          <div className="absolute right-[2%] top-[30%] w-[10%]">
            <ToolsRack tools={tools} active={activeTool} onPick={(n) => pick("tool", n)} />
          </div>
          <div className="absolute bottom-[6%] right-[8%] w-[3.5%]"><Plant /></div>
          <div className="absolute left-[32%] top-[20%] w-[3.5%]"><Plant /></div>

          {/* desks — phone rings on a summons, screen glitches red when the beat failed */}
          {staff.map((a) => {
            const d = layout.desks[a.id];
            const p = poses.get(a.id);
            if (!d) return null;
            return (
              <div key={`desk-${a.id}`}
                className="absolute -translate-x-1/2"
                style={{ left: `${d.x}%`, top: `${d.y + 1.5}%`, zIndex: Math.round(d.y), width: `${13 * layout.deskScale}%` }}>
                <Desk hue={hueOf(a.id)} on={p?.working === true && p.at === "desk"} name={a.name}
                  phone={phoneStateAt(a.id, events, t)}
                  alarm={alarm?.actor === a.id} />
              </div>
            );
          })}

          {/* characters — the customer at the counter, the staff wherever the script puts them */}
          {[...(customer ? [customer] : []), ...staff].map((a, i) => {
            const p = poses.get(a.id);
            if (!p) return null;
            return (
              <Walker key={a.id} actor={a} pose={p} slot={i} scale={layout.deskScale}
                sweat={alarm?.actor === a.id}
                selected={selected?.k === "agent" && selected.id === a.id}
                onClick={() => pick("agent", a.id)} />
            );
          })}
          </div>{/* end of the 🎥 world */}

          {/* room-light overlays — above everything, never interactive */}
          <div className="fleet-crt pointer-events-none absolute inset-0 z-[300]" />
          <div className="fleet-vignette pointer-events-none absolute inset-0 z-[300]" />
          {alarm && !done && <div className="fleet-alarm pointer-events-none absolute inset-0 z-[300]" />}

          {/* end-of-recording finale: the eval verdict takes the stage */}
          {done && <div className="fleet-done pointer-events-none absolute inset-0 z-[310]" />}
          {done && verdict === "PASS" && (
            <div className="pointer-events-none absolute inset-0 z-[320] overflow-hidden">
              {Array.from({ length: 26 }, (_, i) => (
                <span key={i} className="fleet-confetti"
                  style={{
                    left: `${(i * 37) % 100}%`,
                    background: ["#34d399", "#7aa2ff", "#f472b6", "#fbbf24", "#7df0ff"][i % 5],
                    animationDelay: `${(i % 13) * 0.2}s`,
                    animationDuration: `${2.2 + (i % 5) * 0.3}s`,
                  }} />
              ))}
            </div>
          )}
          {done && verdict === "FAIL" && (
            <div className="absolute inset-0 z-[320] grid place-items-center">
              <div className="fleet-stamp rounded-lg bg-ink-950/60 px-8 py-4 text-center backdrop-blur-[1px]">
                <p className="font-mono text-[26px] font-black uppercase tracking-[0.3em]">Evals failed</p>
                {!shared && (
                  <Link href={`/sessions/${encodeURIComponent(threadId)}/evals`}
                    className="pointer-events-auto mt-1 inline-block font-mono text-[11px] tracking-wider text-fail underline underline-offset-4 hover:text-fg">
                    see what failed →
                  </Link>
                )}
              </div>
            </div>
          )}
        </div>

        {/* ── inspect card ── */}
        <aside className="card flex max-h-[560px] flex-col overflow-hidden">
          {sel ? (
            <InspectCard actor={sel} pose={poses.get(sel.id) ?? null} events={events}
              declared={data.declared.find((d) => d.name.toLowerCase() === sel.name.toLowerCase())}
              onClose={() => setSelected(null)} />
          ) : thing ? (
            <StationCard info={thing} nameOf={nameOf} onClose={() => setSelected(null)} />
          ) : (
            <Roster actors={actors} poses={poses} onPick={(id) => pick("agent", id)} declared={data.declared} />
          )}
        </aside>
      </div>

      {/* ── scrubber ── */}
      <Scrubber events={events} total={total} t={t} onSeek={clock.seek} />

      {/* ── transcript: the question and everyone's last word, turn by turn ── */}
      <Transcript turns={turns} t={t} nameOf={nameOf} onSeek={clock.seek} />

      <p className="text-[12px] text-fg-muted">
        Every character is a real agent from the trace — the customer asks at the reception counter,
        the supervisor takes the question and phones ☎ the right teammate, who leaves the break room
        for their desk; knowledge is read at the library, actions run at the tool wall, and each
        agent's last word stays up until they act again. Long pauses are squeezed.
        {!shared && (
          <>
            {" "}
            <Link href={`/sessions/${encodeURIComponent(threadId)}`} className="text-signal hover:underline">
              open the full conversation →
            </Link>
          </>
        )}
      </p>
    </div>
  );
}

/* ── a character that walks between poses ── */
function Walker({ actor, pose, slot, selected, sweat = false, scale = 1, onClick }: {
  actor: ReplayActor; pose: Pose; slot: number; selected: boolean; sweat?: boolean;
  /** Shrinks with a crowded floor (see `layoutOffice.deskScale`) so neighbours stay apart. */
  scale?: number;
  onClick: () => void;
}) {
  // Walk in an L along the aisles instead of cutting diagonally through the desks: a move
  // that changes both axes takes the horizontal corridor leg first, then the vertical one.
  const [shown, setShown] = useState({ x: pose.x, y: pose.y });
  const prevTarget = useRef({ x: pose.x, y: pose.y });
  useEffect(() => {
    const from = prevTarget.current;
    prevTarget.current = { x: pose.x, y: pose.y };
    if (Math.abs(from.x - pose.x) > 3 && Math.abs(from.y - pose.y) > 3) {
      setShown({ x: pose.x, y: from.y });
      const id = setTimeout(() => setShown({ x: pose.x, y: pose.y }), 430);
      return () => clearTimeout(id);
    }
    setShown({ x: pose.x, y: pose.y });
  }, [pose.x, pose.y]);
  const walking = useWalking(shown.x, shown.y);

  const guest = isCustomer(actor);
  const hue = guest ? 42 : hueOf(actor.id);
  // the customer stands alone at the counter — a crowded desk floor never shrinks them
  const size = (guest ? 36 : actor.depth ? 38 : 46) * (guest ? 1 : scale);
  return (
    <button
      onClick={onClick}
      className="absolute -translate-x-1/2 -translate-y-full cursor-pointer transition-all duration-[450ms] ease-in-out"
      // depth order on the floor — but a FRESH word must paint over a neighbour's faded one
      style={{ left: `${shown.x}%`, top: `${shown.y}%`, zIndex: Math.round(pose.y) + 10 + (pose.bubble && !pose.bubble.faded ? 100 : 0) }}
      title={actor.name}
    >
      {pose.bubble && (
        // the greeting supervisor stands high near the counter — their bubble drops below,
        // like the customer's, so it can't clip the stage's top edge
        <BubbleView bubble={pose.bubble} x={shown.x} y={shown.y} beside={guest} scale={scale}
          forceBelow={pose.at === "counter" && !guest} />
      )}
      <div className={clsx(selected && "rounded-lg ring-2 ring-signal/70")}>
        <PixelPerson hue={hue} size={size} walking={walking} working={pose.working && !walking}
          facing={pose.facing} hat={guest} mood={pose.mood ?? null} sweat={sweat} />
      </div>
      <div className="mx-auto -mt-0.5 h-1.5 w-8 rounded-full bg-black/45 blur-[2px]" />
      {walking && (
        <>
          <span className="fleet-puff absolute -bottom-0.5 left-1 h-1.5 w-1.5 rounded-full bg-[#cdb8ff]/50" />
          <span className="fleet-puff2 absolute -bottom-0.5 right-1 h-1 w-1 rounded-full bg-[#cdb8ff]/40" />
        </>
      )}
      <span className={clsx("mt-0.5 inline-flex items-center gap-1 rounded-sm border px-1.5 font-mono text-[9px]",
        guest ? "border-warn/25 bg-ink-950/85 text-warn"
          : pose.working ? "border-signal/30 bg-ink-950/90 text-signal shadow-[0_0_8px_rgba(125,240,255,0.15)]"
          : "border-white/5 bg-ink-950/75 text-fg-faint")}>
        <i className="h-1 w-1 rounded-full" style={{ background: `hsl(${hue} 70% 60%)` }} />
        {actor.name}
      </span>
    </button>
  );
}

function BubbleView({ bubble, x, y, beside = false, forceBelow = false, scale = 1 }: {
  bubble: NonNullable<Pose["bubble"]>; x: number; y: number;
  /** A crowded floor narrows the bubbles too — otherwise neighbours' words overlap. */
  scale?: number;
  /** Hang the bubble to the LEFT of the character (the customer by the door, whose words
   *  must not drop onto the desks below). */
  beside?: boolean;
  /** Hang the bubble BELOW the character (the supervisor at the counter, near the top edge). */
  forceBelow?: boolean;
}) {
  // Keep the bubble inside the office on BOTH axes: right-aligned near the right wall (left
  // near the left), and dropped BELOW the character when they stand near the top — otherwise
  // a long line spills over the roster or is clipped by the stage's top edge.
  const side = x >= 64 ? "right" : x <= 36 ? "left" : "center";
  // only the door zone is close enough to the top to clip; the root row (y≈40) goes UP, or
  // its words land on the sub-agents' bubbles one row down
  const below = (y <= 34 || forceBelow) && !beside;
  const anchor = clsx(
    beside ? "right-full top-0 mr-1.5" : side === "right" ? "right-0" : side === "left" ? "left-0" : "left-1/2 -translate-x-1/2",
    !beside && (below ? "top-full mt-1" : "bottom-full mb-2"),
    // a last word that has been up a while stays readable but stops competing with live beats
    bubble.faded ? "opacity-60" : "fleet-pop",
  );
  const tail = side === "right" ? "ml-auto mr-5" : "ml-5";
  const tailDots = side === "right" ? "ml-auto mr-6" : "ml-6";
  const tailDots2 = side === "right" ? "ml-auto mr-4" : "ml-4";

  // typewriter: while the beat is in flight, only `progress` of the words are on screen
  // widths shrink with the floor; below full scale the cap is a hard px so long lines wrap
  // instead of reaching across a neighbour's desk
  const capped = (px: number) => (scale < 1 ? { maxWidth: `${Math.round(px * scale)}px` } : undefined);
  const typing = bubble.progress !== undefined && bubble.progress < 1;
  const reveal = (text: string) =>
    typing ? text.slice(0, Math.max(1, Math.ceil(text.length * (bubble.progress ?? 1)))) : text;
  const caret = typing && <span className="opacity-70">▌</span>;

  if (bubble.type === "thought") {
    return (
      <div className={clsx("pointer-events-none absolute z-50 w-max max-w-[190px]", anchor)} style={capped(190)}>
        <div className="rounded-[14px] border border-t_think/40 bg-ink-800/95 px-2.5 py-1.5 text-left font-mono text-[10px] leading-snug text-t_think shadow-[2px_2px_0_rgba(10,7,18,0.4)]">
          {reveal(bubble.text)}{caret}
        </div>
        <div className={clsx("mt-0.5 h-2 w-2 rounded-full border border-t_think/40 bg-ink-800/95", tailDots)} />
        <div className={clsx("h-1.5 w-1.5 rounded-full border border-t_think/40 bg-ink-800/95", tailDots2)} />
      </div>
    );
  }
  if (bubble.type === "speech") {
    return (
      <div className={clsx("pointer-events-none absolute z-50 w-max", beside ? "max-w-[168px]" : "max-w-[210px]", anchor)}
        style={beside ? undefined : capped(210)}>
        <div className={clsx("rounded-lg border border-line-bright bg-[#f4f6fb] px-2.5 py-1.5 text-left text-[10.5px] font-medium leading-snug text-ink-900 shadow-[3px_3px_0_rgba(10,7,18,0.45)]",
          bubble.faded && "line-clamp-3")}>
          {reveal(bubble.text)}{caret}
        </div>
        {beside
          ? <div className="absolute right-0 top-3 h-2 w-2 translate-x-1 rotate-45 border-r border-t border-line-bright bg-[#f4f6fb]" />
          : <div className={clsx("h-2 w-2 -translate-y-1 rotate-45 border-b border-r border-line-bright bg-[#f4f6fb]", tail)} />}
      </div>
    );
  }
  if (bubble.type === "error") {
    return (
      <div className={clsx("pointer-events-none absolute bottom-full left-1/2 z-50 mb-1.5 -translate-x-1/2", bubble.faded ? "opacity-60" : "fleet-pop")}>
        <span className="grid h-6 w-6 animate-bounce place-items-center rounded-full bg-fail font-mono text-[13px] font-bold text-ink-950 shadow-[0_0_14px_rgba(251,113,133,0.8)]">!</span>
      </div>
    );
  }
  return (
    <div className={clsx("pointer-events-none absolute z-50 w-max max-w-[200px]", anchor)} style={capped(200)}>
      <span className={clsx("block rounded-md border px-2 py-0.5 text-left font-mono text-[10px] shadow-[2px_2px_0_rgba(10,7,18,0.4)] backdrop-blur-[2px]",
        bubble.icon === "skill" ? "border-t_retriever/50 bg-t_retriever/15 text-t_retriever"
          : bubble.icon === "call" ? "border-t_llm/50 bg-t_llm/15 text-t_llm"
          : "border-t_tool/50 bg-t_tool/15 text-t_tool")}>
        {bubble.icon === "skill" ? "◈" : bubble.icon === "call" ? "→" : "⚙"} {bubble.text}
        {/* a turn that ended on this tool: show what it returned, not just that it ran */}
        {bubble.sub && <span className="block truncate text-[9.5px] text-fg-muted" title={bubble.sub}>→ {bubble.sub}</span>}
      </span>
    </div>
  );
}

/* ── the words, line by line ── */
function WordText({ bubble }: { bubble: Bubble }) {
  if (bubble.type === "error") return <span className="font-mono text-fail">! {bubble.text}</span>;
  if (bubble.type === "chip")
    return (
      <span className={clsx("font-mono", bubble.icon === "skill" ? "text-t_retriever" : bubble.icon === "call" ? "text-t_llm" : "text-t_tool")}>
        {bubble.icon === "skill" ? "◈" : bubble.icon === "call" ? "→" : "⚙"} {bubble.text}
        {bubble.sub && <span className="text-fg-muted"> → {bubble.sub}</span>}
      </span>
    );
  if (bubble.type === "thought") return <span className="italic text-t_think">{bubble.text}</span>;
  return <span>{bubble.text}</span>;
}

/* ── transcript: per turn, the question and every agent's last word ── */
function Transcript({ turns, t, nameOf, onSeek }: {
  turns: TurnDigest[]; t: number; nameOf: (id: string) => string; onSeek: (v: number) => void;
}) {
  if (!turns.length) return null;
  return (
    <div className="card overflow-hidden !p-0">
      <div className="border-b border-line px-4 py-2.5 font-mono text-[11px] uppercase tracking-wider text-fg-muted">
        transcript · {turns.length} turn{turns.length > 1 ? "s" : ""} · last word of every agent
      </div>
      <ol className="divide-y divide-line-soft">
        {turns.map((turn, i) => (
          <li key={turn.trace_id} className={clsx("transition-opacity duration-500", turn.pt > t && "opacity-40")}>
            <button onClick={() => onSeek(turn.pt)} title="jump to this turn"
              className="flex w-full items-start gap-3 px-4 py-2.5 text-left hover:bg-ink-800/40">
              <span className="mt-0.5 shrink-0 font-mono text-[10px] text-fg-faint">#{i + 1}</span>
              <div className="min-w-0 flex-1 space-y-1 text-[12px] leading-snug">
                <p className="flex gap-2">
                  <span className="w-[110px] shrink-0 truncate font-mono text-[10px] leading-[18px] text-warn">customer</span>
                  <span className="min-w-0 font-medium">{turn.ask || <span className="text-fg-faint">(no message recorded)</span>}</span>
                </p>
                {turn.words.map((w) => (
                  <p key={w.actor} className="flex gap-2">
                    <span className="w-[110px] shrink-0 truncate font-mono text-[10px] leading-[18px] text-fg-muted" title={nameOf(w.actor)}>
                      {nameOf(w.actor)}
                    </span>
                    <span className="min-w-0 text-fg"><WordText bubble={w.bubble} /></span>
                  </p>
                ))}
                {!turn.words.length && <p className="text-[11px] text-fg-faint">no agent said anything this turn</p>}
              </div>
            </button>
          </li>
        ))}
      </ol>
    </div>
  );
}

/* ── side panel: roster or inspect ── */
function Roster({ actors, poses, onPick, declared }: {
  actors: ReplayActor[]; poses: Map<string, Pose>; onPick: (id: string) => void; declared: Declared[];
}) {
  return (
    <>
      <div className="border-b border-line px-4 py-2.5 font-mono text-[11px] uppercase tracking-wider text-fg-muted">
        the team · {actors.filter((a) => !isCustomer(a)).length}
      </div>
      <div className="flex-1 space-y-1 overflow-y-auto p-2">
        {actors.map((a) => {
          const p = poses.get(a.id);
          const doing = p?.action;
          return (
            <button key={a.id} onClick={() => onPick(a.id)}
              className="flex w-full items-center gap-2.5 rounded-lg border border-transparent px-2 py-1.5 text-left transition-colors hover:border-line hover:bg-ink-800/60">
              <div className="w-7 shrink-0"><PixelPerson hue={isCustomer(a) ? 42 : hueOf(a.id)} size={26} hat={isCustomer(a)} /></div>
              <div className="min-w-0 flex-1">
                <p className={clsx("truncate text-[12.5px] font-semibold", isCustomer(a) && "text-warn")} style={{ paddingLeft: a.depth * 8 }}>
                  {a.depth > 0 && <span className="text-fg-faint">└ </span>}{a.name}
                </p>
                <p className="truncate font-mono text-[9.5px] text-fg-faint">
                  {isCustomer(a)
                    ? (p?.bubble?.type === "speech" ? `“${p.bubble.text}”` : "waiting")
                    : doing ? `${doing.kind}: ${doing.name}` : p?.working ? "working" : "idle"}
                </p>
              </div>
              {a.errors > 0 && <span className="font-mono text-[9px] text-fail">{a.errors}!</span>}
            </button>
          );
        })}
        {declared.length > 0 && (
          <p className="px-2 pt-2 font-mono text-[9.5px] leading-relaxed text-fg-faint">
            click anyone for their card — or a book on the library / a tool on the wall.
          </p>
        )}
      </div>
    </>
  );
}

function InspectCard({ actor, pose, events, declared, onClose }: {
  actor: ReplayActor; pose: Pose | null; events: PlayEvent[]; declared?: Declared; onClose: () => void;
}) {
  const mine = events.filter((e) => e.actor === actor.id);
  const models = [...new Set(mine.map((e) => e.model).filter(Boolean))];
  const work = mine.filter((e) => !e.container);
  const tokSum = work.reduce((n, e) => n + (e.tokens ?? 0), 0);
  const usdSum = work.reduce((n, e) => n + (e.cost ?? 0), 0);
  const toolCounts = new Map<string, number>();
  for (const e of mine) if (e.kind === "tool" || e.kind === "skill") toolCounts.set(e.name, (toolCounts.get(e.name) ?? 0) + 1);
  return (
    <>
      <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
        <span className="font-mono text-[11px] uppercase tracking-wider text-fg-muted">personnel file</span>
        <button onClick={onClose} className="text-[12px] text-fg-faint hover:text-fg">✕</button>
      </div>
      <div className="flex-1 overflow-y-auto p-4">
        <div className="mx-auto w-16 rounded-lg border border-line bg-ink-800 p-2">
          <PixelPerson hue={isCustomer(actor) ? 42 : hueOf(actor.id)} size={48} working={pose?.working} hat={isCustomer(actor)} />
        </div>
        <p className="mt-2 text-center text-[15px] font-bold">{actor.name}</p>
        <p className="text-center font-mono text-[9.5px] uppercase tracking-wider text-fg-faint">
          {isCustomer(actor) ? "the person this office works for" : actor.kind === "subagent" ? "sub-agent" : "agent"}
        </p>
        {pose?.bubble && (
          <p className="mt-3 rounded-lg border border-line-soft bg-ink-800/60 p-2.5 text-[11.5px] leading-relaxed text-fg">
            <span className="mb-1 block font-mono text-[9.5px] uppercase tracking-wider text-fg-faint">
              {isCustomer(actor) ? "asked" : pose.working ? "now" : "last word"}
            </span>
            <WordText bubble={pose.bubble} />
          </p>
        )}
        {declared?.description && (
          <p className="mt-3 rounded-lg border border-line-soft bg-ink-800/60 p-2.5 text-[11.5px] leading-relaxed text-fg-muted">
            {declared.description}
          </p>
        )}
        <dl className="mt-3 space-y-1.5 font-mono text-[10.5px]">
          <Row k="status" v={pose?.working ? "● working" : "idle"} accent={pose?.working ? "text-ok" : "text-fg-faint"} />
          <Row k="now" v={pose?.action ? pose.action.name : "—"} />
          <Row k="wraps" v={models.join(", ") || "—"} />
          <Row k="steps" v={String(actor.events)} />
          {tokSum > 0 && <Row k="tokens" v={tokSum.toLocaleString()} />}
          {usdSum > 0 && <Row k="cost" v={`$${usdSum.toFixed(4)}`} />}
          {actor.errors > 0 && <Row k="failures" v={String(actor.errors)} accent="text-fail" />}
        </dl>
        {(declared?.tools.length || toolCounts.size) ? (
          <div className="mt-3">
            <p className="mb-1 font-mono text-[9.5px] uppercase tracking-wider text-fg-faint">tools</p>
            <div className="flex flex-wrap gap-1">
              {[...new Set([...(declared?.tools ?? []).map((d) => d.name), ...toolCounts.keys()])].map((tl) => (
                <span key={tl} className={clsx("rounded border px-1.5 py-0.5 font-mono text-[9.5px]",
                  toolCounts.has(tl) ? "border-t_tool/40 bg-t_tool/10 text-t_tool" : "border-line text-fg-faint")}>
                  {tl}{toolCounts.has(tl) ? ` ×${toolCounts.get(tl)}` : ""}
                </span>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </>
  );
}

/* ── side panel: a thing instead of a person (a book, a tool) ── */
function StationCard({ info, nameOf, onClose }: {
  info: StationInfo; nameOf: (id: string) => string; onClose: () => void;
}) {
  const skill = info.kind === "skill";
  return (
    <>
      <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
        <span className="font-mono text-[11px] uppercase tracking-wider text-fg-muted">
          {skill ? "library card" : "tool sheet"}
        </span>
        <button onClick={onClose} className="text-[12px] text-fg-faint hover:text-fg">✕</button>
      </div>
      <div className="flex-1 overflow-y-auto p-4">
        <div className={clsx("mx-auto grid h-14 w-14 place-items-center rounded-lg border text-[22px]",
          skill ? "border-t_retriever/40 bg-t_retriever/10 text-t_retriever" : "border-t_tool/40 bg-t_tool/10 text-t_tool")}>
          {skill ? "◈" : "⚙"}
        </div>
        <p className="mt-2 break-words text-center font-mono text-[13.5px] font-bold">{info.name}</p>
        <p className="text-center font-mono text-[9.5px] uppercase tracking-wider text-fg-faint">
          {skill ? "skill · read at the library" : "tool · run at the wall"}
        </p>
        <p className="mt-3 rounded-lg border border-line-soft bg-ink-800/60 p-2.5 text-[11.5px] leading-relaxed text-fg-muted"
          title={info.description}>
          {info.description
            ? <span className="line-clamp-4">{info.description}</span>
            : <span className="text-fg-faint">no description declared for this {info.kind}.</span>}
        </p>
        <dl className="mt-3 space-y-1.5 font-mono text-[10.5px]">
          <Row k="runs" v={info.runs ? `${info.runs}×` : "never in this conversation"}
            accent={info.runs ? undefined : "text-fg-faint"} />
          <Row k="used by" v={info.by.map(nameOf).join(", ") || "—"} />
          {info.failures > 0 && <Row k="failures" v={String(info.failures)} accent="text-fail" />}
        </dl>
        {info.lastResult && info.lastResult !== info.description && (
          <div className="mt-3">
            <p className="mb-1 font-mono text-[9.5px] uppercase tracking-wider text-fg-faint">last result</p>
            <p className="line-clamp-3 rounded-lg border border-line-soft bg-ink-800/60 p-2.5 font-mono text-[10.5px] leading-relaxed text-fg-muted"
              title={info.lastResult}>
              {info.lastResult}
            </p>
          </div>
        )}
      </div>
    </>
  );
}

function Row({ k, v, accent }: { k: string; v: string; accent?: string }) {
  return (
    <div className="flex justify-between gap-3 border-b border-dashed border-line-soft pb-1">
      <dt className="uppercase tracking-wider text-fg-faint">{k}</dt>
      <dd className={clsx("truncate text-right", accent ?? "text-fg")}>{v}</dd>
    </div>
  );
}

/* ── bottom scrubber with event ticks ── */
function Scrubber({ events, total, t, onSeek }: {
  events: PlayEvent[]; total: number; t: number; onSeek: (v: number) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const seek = (clientX: number) => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    onSeek(Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * total);
  };
  // The tick layer is static per script — rebuilding every event marker on each 25fps commit
  // was pure reconciliation waste. Only the fill + playhead follow `t`.
  const ticks = useMemo(
    () =>
      events
        .filter((e) => !e.container)
        .map((e) => (
          <span key={`${e.trace_id}:${e.span_id}`}
            className="absolute top-1/2 h-3 w-[3px] -translate-y-1/2 rounded-full opacity-80"
            style={{
              left: `${(e.pt / total) * 100}%`,
              background: e.status === "error" ? "#fb7185" : e.delegate_to ? "#7df0ff" : "#39435c",
            }} />
        )),
    [events, total],
  );
  return (
    <div ref={ref} onMouseDown={(e) => seek(e.clientX)} className="card relative h-9 cursor-pointer overflow-hidden !p-0">
      <div className="absolute inset-y-0 left-0 bg-signal/10" style={{ width: `${(t / total) * 100}%` }} />
      {ticks}
      <div className="absolute top-0 h-full w-[2px] bg-signal shadow-glow" style={{ left: `${(t / total) * 100}%` }} />
    </div>
  );
}
