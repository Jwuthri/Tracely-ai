"use client";

import clsx from "clsx";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useId, useMemo, useState } from "react";
import type { AgentRow, Scenario, ScenarioTurn } from "@/app/lib/api";
import { listJudgeModels, type JudgeModelOption } from "@/app/lib/evaluators";
import { AgentPicker } from "./AgentPicker";
import { EndpointPanel } from "./EndpointPanel";
import { TurnEditor, emptyTurn } from "./TurnEditor";
import { Toggle } from "./Toggle";
import { Badge } from "./ui";
import { IconChevron, IconGate, IconPlay, IconTrash } from "./icons";

const FIELD =
  "w-full rounded-lg border border-line bg-ink-700 px-2.5 py-2 font-mono text-[12.5px] text-fg placeholder:text-fg-faint transition-colors hover:border-line-bright focus:border-signal/50 focus:outline-none";
const LABEL = "font-mono text-[10px] uppercase tracking-wider text-fg-faint";

export function ScenariosManager({
  agents,
  initial,
  counts = {},
}: {
  agents: AgentRow[];
  initial: Scenario[];
  counts?: Record<string, number>;
}) {
  const [agentId, setAgentId] = useState(agents[0]?.id ?? "");
  const [rows, setRows] = useState(initial);
  const [configured, setConfigured] = useState(false);
  const [composing, setComposing] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const router = useRouter();

  const agent = agents.find((a) => a.id === agentId) ?? agents[0];
  const mine = useMemo(() => rows.filter((s) => s.agent_id === agent?.id), [rows, agent]);
  const enabled = mine.filter((s) => s.enabled).length;

  // Stable identity: EndpointPanel takes this as an effect dependency, so an inline arrow would
  // re-run the fetch on every render of this component.
  const onConfigured = useCallback((c: boolean) => setConfigured(c), []);

  async function patch(id: string, body: Record<string, unknown>) {
    const r = await fetch(`/api/scenarios/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      // A silently ignored toggle reads as "it worked" while the suite keeps its old shape.
      const d = await r.json().catch(() => null);
      setErr(d?.detail ?? `Could not update the scenario (HTTP ${r.status}).`);
      return;
    }
    const updated: Scenario = await r.json();
    setRows((prev) => prev.map((s) => (s.id === id ? updated : s)));
  }

  /** Open the inline editor for a scenario. Closing the composer first keeps exactly one form
   *  open, so the page never shows two competing sets of unsaved edits. */
  function openEditor(id: string) {
    setComposing(false);
    setEditingId(id);
  }

  // Scenario id currently being launched — a per-row flag, not a page-wide one, so running one
  // scenario doesn't freeze the buttons on the others.
  const [running, setRunning] = useState<string | null>(null);

  async function runScenario(s: Scenario) {
    setRunning(s.id);
    setErr(null);
    try {
      const r = await fetch(`/api/scenarios/${s.id}/run`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ env: "ci" }),
      });
      const d = await r.json().catch(() => null);
      if (r.ok && d?.conversation_id) {
        // Straight to the conversation: it exists the moment the run is queued and fills in turn
        // by turn, which is the thing worth watching.
        router.push(`/sessions/${encodeURIComponent(d.conversation_id)}`);
        return;
      }
      setErr(d?.detail ?? `Could not run the scenario (HTTP ${r.status})`);
    } catch {
      setErr("Could not run the scenario: the server is unreachable.");
    } finally {
      setRunning(null);
    }
  }

  async function remove(s: Scenario) {
    if (!confirm(`Delete scenario "${s.title || s.id}"?`)) return;
    const r = await fetch(`/api/scenarios/${s.id}`, { method: "DELETE" });
    if (r.ok) setRows((prev) => prev.filter((x) => x.id !== s.id));
  }

  async function runGate() {
    if (!agent) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await fetch("/api/gate/simulate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ agent: agent.slug, env: "ci" }),
      });
      const d = await r.json().catch(() => null);
      if (r.ok && d?.id) {
        router.push(`/gates/${d.id}`);
        return;
      }
      setErr(d?.detail ?? `Could not start the gate (HTTP ${r.status})`);
    } catch {
      setErr("Could not start the gate: the server is unreachable.");
    } finally {
      setBusy(false);
    }
  }

  if (!agent) return null;

  return (
    <div className="space-y-5">
      <div className="reveal flex flex-wrap items-end justify-between gap-4">
        <div>
          <label className={LABEL} htmlFor="sc-agent">
            Agent
          </label>
          <AgentPicker
            id="sc-agent"
            agents={agents}
            value={agentId}
            onChange={setAgentId}
            hint={(a) => `${counts[a.id] ?? 0} scenarios`}
            sort={false}
            className="mt-1 block w-56 rounded-lg border border-line bg-ink-700 px-2.5 py-2 font-mono text-[12.5px] text-fg transition-colors hover:border-line-bright focus:border-signal/50 focus:outline-none"
          />
        </div>
        <div className="flex flex-col items-end gap-1.5">
          <button
            onClick={runGate}
            disabled={busy || !configured || enabled === 0}
            className="btn-primary"
          >
            <IconGate className="h-4 w-4" />
            {busy ? "Starting…" : `Run ${enabled} scenario${enabled === 1 ? "" : "s"} · ci`}
          </button>
          {!configured && (
            <p className="text-[12px] text-fg-faint">Register the endpoint to enable this.</p>
          )}
          {configured && enabled === 0 && (
            <p className="text-[12px] text-fg-faint">No enabled scenarios for this agent yet.</p>
          )}
          {err && (
            <p role="alert" className="text-[12px] text-fail">
              {err}
            </p>
          )}
        </div>
      </div>

      <div className="reveal" style={{ animationDelay: "60ms" }}>
        <EndpointPanel agentSlug={agent.slug} onConfigured={onConfigured} />
      </div>

      <section className="reveal card overflow-hidden" style={{ animationDelay: "100ms" }}>
        <div className="flex items-center justify-between border-b border-line px-4 py-3">
          <span className="text-[13px] font-semibold text-fg">
            Conversations{" "}
            <span className="font-mono text-[11px] text-fg-faint">
              ({mine.length}, {enabled} enabled)
            </span>
          </span>
          <button
            onClick={() => {
              setEditingId(null); // one form open at a time
              setComposing((c) => !c);
            }}
            className="btn-ghost text-[12.5px]"
          >
            {composing ? "Cancel" : "+ New scenario"}
          </button>
        </div>

        {composing && (
          <ScenarioForm
            agentSlug={agent.slug}
            onSaved={(s) => {
              setRows((prev) => [s, ...prev]);
              setComposing(false);
            }}
            onCancel={() => setComposing(false)}
          />
        )}

        {mine.length === 0 && !composing ? (
          <div className="px-4 py-14 text-center text-[13px] text-fg-faint">
            No scenarios for <span className="font-mono text-fg-muted">{agent.slug}</span> yet — add
            one, or open a conversation under Traces and use{" "}
            <span className="text-fg-muted">Save as scenario</span>.
          </div>
        ) : (
          mine.map((s) =>
            editingId === s.id ? (
              <ScenarioForm
                key={s.id}
                agentSlug={agent.slug}
                scenario={s}
                onSaved={(updated) => {
                  setRows((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
                  setEditingId(null);
                }}
                onCancel={() => setEditingId(null)}
              />
            ) : (
            <div
              key={s.id}
              role="button"
              tabIndex={0}
              onClick={() => openEditor(s.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  openEditor(s.id);
                }
              }}
              className="group grid cursor-pointer grid-cols-[86px_1fr_auto] items-start gap-3 border-b border-line/50 px-4 py-3 transition-colors last:border-0 hover:bg-hilite/[0.025] focus-visible:bg-hilite/[0.025] focus-visible:outline-none"
            >
              <Badge variant={s.kind === "ADVERSARIAL" ? "warn" : "info"}>
                {s.kind === "ADVERSARIAL" ? "attack" : "scripted"}
              </Badge>
              <div className="min-w-0">
                <div className={clsx("text-[13px]", s.enabled ? "text-fg" : "text-fg-faint")}>
                  {s.title || <span className="text-fg-faint">(untitled)</span>}
                </div>
                <div className="mt-1 font-mono text-[11.5px] text-fg-faint">
                  {s.kind === "ADVERSARIAL"
                    ? `goal: ${s.goal.slice(0, 90)}${s.goal.length > 90 ? "…" : ""} · ≤${s.max_turns} turns`
                    : `${s.turns.length} turn${s.turns.length === 1 ? "" : "s"}${
                        expectationCount(s) ? ` · ${expectationCount(s)} expectation${expectationCount(s) === 1 ? "" : "s"}` : ""
                      }`}
                  {s.source_thread_id && (
                    <>
                      {" · "}
                      <a
                        href={`/sessions/${s.source_thread_id}`}
                        onClick={(e) => e.stopPropagation()}
                        className="text-fg-muted underline decoration-line-bright underline-offset-2 transition-colors hover:text-signal"
                      >
                        from production
                      </a>
                    </>
                  )}
                </div>
                {s.kind === "SCRIPTED" && s.turns.length > 0 && (
                  <div className="mt-1.5 truncate font-mono text-[11.5px] text-fg-muted">
                    &ldquo;{s.turns[0].message.slice(0, 110)}
                    {s.turns[0].message.length > 110 ? "…" : ""}&rdquo;
                  </div>
                )}
              </div>
              {/* Actions live inside a clickable row, so every one of them stops propagation —
                  otherwise toggling `enabled` or deleting would also open the editor. */}
              <div
                className="flex items-center gap-2"
                onClick={(e) => e.stopPropagation()}
                onKeyDown={(e) => e.stopPropagation()}
              >
                <button
                  onClick={() => runScenario(s)}
                  disabled={running !== null}
                  title="Drive this conversation against the agent endpoint now"
                  className="rounded-md border border-signal/40 bg-signal/10 px-2.5 py-1 text-[12px] font-medium text-signal transition-colors hover:bg-signal/20 disabled:opacity-50"
                >
                  {running === s.id ? "Running…" : "Run"}
                </button>
                <Toggle
                  checked={s.enabled}
                  onChange={(next) => patch(s.id, { enabled: next })}
                  label={s.enabled ? "enabled" : "off"}
                />
                <button
                  onClick={() => remove(s)}
                  aria-label={`Delete ${s.title || "scenario"}`}
                  title="Delete scenario"
                  className="grid h-7 w-7 place-items-center rounded-md border border-transparent text-fg-faint opacity-0 transition-all hover:border-fail/30 hover:bg-fail/10 hover:text-fail focus-visible:opacity-100 focus-visible:outline-none group-hover:opacity-100"
                >
                  <IconTrash className="h-[15px] w-[15px]" />
                </button>
                <IconChevron className="h-4 w-4 text-fg-faint transition-colors group-hover:text-signal" />
              </div>
            </div>
            ),
          )
        )}
      </section>
    </div>
  );
}

/** Authors a scenario, and edits one.
 *
 *  Deliberately one component for both: create and edit differ only in where the initial state
 *  comes from and which verb it sends. A second near-identical form is how the two drift — the
 *  editor quietly missing a field the composer grew. */
function ScenarioForm({
  agentSlug,
  scenario,
  onSaved,
  onCancel,
}: {
  agentSlug: string;
  scenario?: Scenario;
  onSaved: (s: Scenario) => void;
  onCancel?: () => void;
}) {
  const editing = Boolean(scenario);
  // Unique per form instance: the create form and an open editor coexist on this page, so fixed
  // ids would duplicate and every label would point at whichever field mounted first.
  const uid = useId();
  const [kind, setKind] = useState<"SCRIPTED" | "ADVERSARIAL">(scenario?.kind ?? "SCRIPTED");
  const [title, setTitle] = useState(scenario?.title ?? "");
  const [turns, setTurns] = useState<ScenarioTurn[]>(
    scenario?.turns?.length ? scenario.turns.map((t) => ({ ...t, tools: [...t.tools] })) : [emptyTurn()],
  );
  const [goal, setGoal] = useState(scenario?.goal ?? "");
  const [maxTurns, setMaxTurns] = useState(scenario?.max_turns ?? 6);
  const [attackerModel, setAttackerModel] = useState(scenario?.attacker_model ?? "");
  // Same curated list the evaluator columns pick their judge from — one source for "models this
  // project can actually call". Fetched only once the adversarial half is on screen.
  const [models, setModels] = useState<JudgeModelOption[]>([]);
  useEffect(() => {
    if (kind !== "ADVERSARIAL" || models.length) return;
    void listJudgeModels().then((m) => setModels(m.models)).catch(() => {});
  }, [kind, models.length]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const filled = turns.filter((t) => t.message.trim());
  const withExpectations = filled.filter((t) => t.expect.trim() || t.tools.length).length;
  const ready = kind === "SCRIPTED" ? filled.length > 0 : goal.trim().length > 0;

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      const payload = {
        title,
        kind,
        turns: filled,
        goal,
        max_turns: kind === "ADVERSARIAL" ? maxTurns : filled.length,
        // Only meaningful for adversarial; harmless (and cleared) for scripted.
        attacker_model: kind === "ADVERSARIAL" ? attackerModel.trim() : "",
      };
      const r = await fetch(
        editing ? `/api/scenarios/${scenario!.id}` : "/api/scenarios",
        {
          method: editing ? "PATCH" : "POST",
          headers: { "content-type": "application/json" },
          // `agent` is only meaningful on create — a scenario never moves between agents.
          body: JSON.stringify(editing ? payload : { ...payload, agent: agentSlug }),
        },
      );
      const d = await r.json().catch(() => null);
      if (!r.ok) {
        setErr(d?.detail ?? `Could not save (HTTP ${r.status})`);
        return;
      }
      onSaved(d as Scenario);
    } catch {
      setErr("Could not save: the server is unreachable.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3 border-b border-line bg-ink-900/40 px-4 py-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex gap-1 rounded-lg border border-line bg-ink-700 p-1">
          {(["SCRIPTED", "ADVERSARIAL"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setKind(k)}
              className={clsx(
                "rounded-md px-3 py-1.5 font-mono text-[11.5px] transition-colors",
                kind === k ? "bg-signal/15 text-fg" : "text-fg-faint hover:text-fg-muted",
              )}
            >
              {k === "SCRIPTED" ? "Scripted" : "Adversarial"}
            </button>
          ))}
        </div>
        <div className="min-w-[220px] flex-1">
          <label className={LABEL} htmlFor={`${uid}-title`}>
            Title
          </label>
          <input
            id={`${uid}-title`}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder={kind === "SCRIPTED" ? "Refund after failed delivery" : "Leak the system prompt"}
            className={`${FIELD} mt-1`}
          />
        </div>
      </div>

      {kind === "SCRIPTED" ? (
        <div>
          <TurnEditor turns={turns} onChange={setTurns} idPrefix={uid} />
          <p className="mt-2 text-[12px] leading-relaxed text-fg-faint">
            {filled.length} turn{filled.length === 1 ? "" : "s"} · replayed in order, each one
            seeing the agent&apos;s previous replies.{" "}
            {withExpectations === 0
              ? "With no expectations set, turns are graded by your evaluators — the same ones that grade production."
              : `${withExpectations} turn${withExpectations === 1 ? " has" : "s have"} expectations, checked on top of your evaluators.`}
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          <div>
            <label className={LABEL} htmlFor={`${uid}-goal`}>
              Attacker goal
            </label>
            <textarea
              id={`${uid}-goal`}
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              rows={3}
              placeholder="Get the agent to reveal another customer's order details."
              className={`${FIELD} mt-1 resize-y leading-relaxed`}
            />
          </div>
          <div className="flex flex-wrap gap-3">
            <div className="w-32">
              <label className={LABEL} htmlFor={`${uid}-max`}>
                Max turns
              </label>
              <input
                id={`${uid}-max`}
                type="number"
                min={1}
                max={20}
                value={maxTurns}
                onChange={(e) => setMaxTurns(Number(e.target.value))}
                className={`${FIELD} mt-1`}
              />
            </div>
            <div className="min-w-[220px] flex-1">
              <label className={LABEL} htmlFor={`${uid}-model`}>
                Attacker model
              </label>
              <select
                id={`${uid}-model`}
                value={attackerModel}
                onChange={(e) => setAttackerModel(e.target.value)}
                className={`${FIELD} mt-1`}
              >
                <option value="">Server default</option>
                {models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
                {/* A model saved earlier (or set by hand) that the curated list no longer
                    carries — keep it selectable so editing a scenario can't silently retarget
                    the attacker. */}
                {attackerModel && !models.some((m) => m.id === attackerModel) && (
                  <option value={attackerModel}>{attackerModel}</option>
                )}
              </select>
            </div>
          </div>
          <p className="text-[12px] text-fg-faint">
            An attacker model improvises each turn toward the goal, adapting to what the agent
            replies. Needs an LLM key — without one the scenario is skipped, never silently passed.
          </p>
        </div>
      )}

      <div className="flex items-center gap-3">
        <button onClick={save} disabled={busy || !ready} className="btn-primary">
          <IconPlay className="h-4 w-4" />
          {busy ? "Saving…" : editing ? "Save changes" : "Create scenario"}
        </button>
        {onCancel && (
          <button onClick={onCancel} disabled={busy} className="btn-ghost text-[12.5px]">
            Cancel
          </button>
        )}
        {err && (
          <p role="alert" className="text-[12px] text-fail">
            {err}
          </p>
        )}
      </div>
    </div>
  );
}

/** How many of a scenario's turns carry an authored expectation — shown so the list makes the
 *  difference between "graded by evaluators only" and "checked against a contract" visible. */
function expectationCount(s: Scenario): number {
  return s.turns.filter((t) => t.expect || t.tools.length).length;
}
