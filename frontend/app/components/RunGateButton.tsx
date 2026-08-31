"use client";

import clsx from "clsx";
import { useRouter } from "next/navigation";
import { useMemo, useState, type ReactNode } from "react";
import { AgentPicker } from "./AgentPicker";
import { IconChevron, IconGate } from "./icons";
import { Badge } from "./ui";
import type { AgentRow } from "@/app/lib/api";

/** What the launcher can run for an agent: a promoted regression case, or an enabled scenario. */
export type GatePick = { id: string; agent_id: string; title: string };
export type GateScenarioPick = GatePick & { kind: "SCRIPTED" | "ADVERSARIAL" };

/** Run the regression suite for one agent — all of it, or the subset you tick.
 *
 *  Two endpoints behind one button: with no scenario picked this is the synchronous replay gate
 *  (`/api/gate`), with one it's the two-phase simulated gate (`/api/gate/simulate`), which also
 *  replays the picked cases. `agents` comes from the project's registry (ordered by the page so
 *  agents that actually have promoted cases come first) — no hardcoded slug, which is what used to
 *  make this button 404 with "agent 'planner' not found" on every fresh project. */
export function RunGateButton({
  agents,
  caseCounts = {},
  cases = [],
  scenarios = [],
}: {
  agents: AgentRow[];
  caseCounts?: Record<string, number>;
  cases?: GatePick[];
  scenarios?: GateScenarioPick[];
}) {
  const [agentId, setAgentId] = useState(agents[0]?.id ?? "");
  // Deselected ids. Everything runs unless you say otherwise, so an agent with a fresh suite
  // behaves exactly as before this picker existed.
  const [off, setOff] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const router = useRouter();

  const agent = agents.find((a) => a.id === agentId) ?? agents[0];
  const myCases = useMemo(() => cases.filter((c) => c.agent_id === agent?.id), [cases, agent]);
  const myScenarios = useMemo(
    () => scenarios.filter((s) => s.agent_id === agent?.id),
    [scenarios, agent],
  );
  const pickedCases = myCases.filter((c) => !off.has(c.id));
  const pickedScenarios = myScenarios.filter((s) => !off.has(s.id));
  const picked = pickedCases.length + pickedScenarios.length;
  // by_agent counts every promoted case; `cases` is one page of them. Only relevant to say so
  // when it actually got cut off.
  const hiddenCases = Math.max(0, (caseCounts[agent?.id ?? ""] ?? 0) - myCases.length);

  if (!agent) {
    return (
      <p className="max-w-xs text-right text-[12.5px] text-fg-faint">
        No agents yet — send a trace, then promote a failing run to a case.
      </p>
    );
  }

  function toggle(id: string) {
    setOff((prev) => {
      const next = new Set(prev);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }

  function setAll(items: { id: string }[], on: boolean) {
    setOff((prev) => {
      const next = new Set(prev);
      for (const i of items) {
        if (on) next.delete(i.id);
        else next.add(i.id);
      }
      return next;
    });
  }

  async function go() {
    if (!agent) return;
    setBusy(true);
    setErr(null);
    try {
      const body: Record<string, unknown> = { agent: agent.slug, env: "ci" };
      // Send ids only when the pick is a real subset — omitting them means "the whole half",
      // which stays right even if this page only listed the first page of cases.
      if (pickedCases.length < myCases.length) body.case_ids = pickedCases.map((c) => c.id);
      const simulate = pickedScenarios.length > 0;
      if (simulate && pickedScenarios.length < myScenarios.length) {
        body.scenario_ids = pickedScenarios.map((s) => s.id);
      }
      const r = await fetch(simulate ? "/api/gate/simulate" : "/api/gate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json().catch(() => null);
      if (r.ok && d?.id) {
        router.push(`/gates/${d.id}`);
        return;
      }
      setErr(d?.detail ?? `Gate failed (HTTP ${r.status})`);
    } catch {
      setErr("Gate failed: could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  const summary = [
    `${pickedCases.length}${pickedCases.length < myCases.length ? ` of ${myCases.length}` : ""} case${myCases.length === 1 ? "" : "s"}`,
    `${pickedScenarios.length}${pickedScenarios.length < myScenarios.length ? ` of ${myScenarios.length}` : ""} scenario${myScenarios.length === 1 ? "" : "s"}`,
  ].join(" · ");

  return (
    <div className="flex flex-col items-end gap-1.5">
      <div className="flex items-center gap-2">
        {agents.length > 1 && (
          <AgentPicker
            agents={agents}
            value={agentId}
            onChange={(v) => {
              setAgentId(v);
              setOff(new Set()); // a pick belongs to the agent it was made for
            }}
            hint={(a) => `${caseCounts[a.id] ?? 0} cases`}
            sort={false}
            id="gate-agent"
            ariaLabel="Agent to gate"
            className="w-52 rounded-lg border border-line bg-ink-700 px-2.5 py-2 font-mono text-[12.5px] text-fg transition-colors hover:border-line-bright focus:border-signal/50 focus:outline-none"
          />
        )}
        <button
          onClick={go}
          disabled={busy || picked === 0}
          className="btn-primary"
        >
          <IconGate className="h-4 w-4" />
          {busy ? "Running gate…" : `Run gate · ${agent.slug} · ci`}
        </button>
      </div>

      {myCases.length + myScenarios.length > 0 && (
        <button
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="flex items-center gap-1 text-[12px] text-fg-faint transition-colors hover:text-fg-muted"
        >
          <IconChevron className={clsx("h-3.5 w-3.5 transition-transform", open ? "rotate-90" : "")} />
          {summary}
        </button>
      )}

      {open && (
        <div className="card w-[min(30rem,88vw)] overflow-hidden text-left">
          <Section
            label="Regression cases"
            items={myCases}
            off={off}
            toggle={toggle}
            setAll={setAll}
            empty="Nothing promoted yet — promote a failing trace to gate on it."
            note={hiddenCases > 0 ? `+${hiddenCases} more not listed — leave every box ticked to run all of them.` : undefined}
          />
          <Section
            label="Scenarios"
            items={myScenarios}
            off={off}
            toggle={toggle}
            setAll={setAll}
            empty="No enabled scenarios — author one under Scenarios."
            note={
              pickedScenarios.length > 0
                ? "Scenarios call your agent's endpoint for real, so this run finishes asynchronously."
                : undefined
            }
            badge={(s) => (
              <Badge variant={s.kind === "ADVERSARIAL" ? "warn" : "info"}>
                {s.kind === "ADVERSARIAL" ? "attack" : "scripted"}
              </Badge>
            )}
          />
        </div>
      )}

      {picked === 0 && myCases.length + myScenarios.length > 0 && (
        <p className="text-[12px] text-fg-faint">Pick at least one case or scenario.</p>
      )}
      {myCases.length + myScenarios.length === 0 && !err && (
        // total == 0 → PASS (nothing to protect yet), so say that rather than let the run look
        // like a real green.
        <p className="max-w-sm text-right text-[12px] text-fg-faint">
          Nothing to run for <span className="font-mono">{agent.slug}</span> — the gate passes with
          nothing to check. Promote a failing trace, or enable a scenario.
        </p>
      )}
      {err && (
        <p role="alert" className="text-[12px] text-fail">
          {err}
        </p>
      )}
    </div>
  );
}

function Section<T extends GatePick>({
  label,
  items,
  off,
  toggle,
  setAll,
  empty,
  note,
  badge,
}: {
  label: string;
  items: T[];
  off: Set<string>;
  toggle: (id: string) => void;
  setAll: (items: { id: string }[], on: boolean) => void;
  empty: string;
  note?: string;
  badge?: (item: T) => ReactNode;
}) {
  const on = items.filter((i) => !off.has(i.id)).length;
  return (
    <section className="border-b border-line last:border-0">
      <div className="flex items-center justify-between bg-ink-900/50 px-3 py-2">
        <span className="font-mono text-[10.5px] uppercase tracking-wider text-fg-faint">
          {label} ({on}/{items.length})
        </span>
        {items.length > 0 && (
          <button
            onClick={() => setAll(items, on < items.length)}
            className="text-[11.5px] text-fg-faint transition-colors hover:text-fg-muted"
          >
            {on < items.length ? "select all" : "clear"}
          </button>
        )}
      </div>
      {items.length === 0 ? (
        <p className="px-3 py-3 text-[12px] text-fg-faint">{empty}</p>
      ) : (
        <ul className="max-h-56 overflow-y-auto">
          {items.map((i) => (
            <li key={i.id}>
              <label className="flex cursor-pointer items-center gap-2.5 px-3 py-2 transition-colors hover:bg-hilite/[0.025]">
                <input
                  type="checkbox"
                  checked={!off.has(i.id)}
                  onChange={() => toggle(i.id)}
                  className="accent-signal"
                />
                {badge?.(i)}
                <span className="min-w-0 truncate text-[12.5px] text-fg">
                  {i.title || <span className="text-fg-faint">(untitled)</span>}
                </span>
              </label>
            </li>
          ))}
        </ul>
      )}
      {note && <p className="px-3 pb-2 text-[11.5px] text-fg-faint">{note}</p>}
    </section>
  );
}
