"use client";

import clsx from "clsx";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useId, useState } from "react";
import type { AgentRow } from "@/app/lib/api";
import {
  formatRate,
  type Audit,
  type AuditCatalog,
  type AuditPreset,
  type TargetTool,
} from "@/app/lib/audits";
import { listJudgeModels, type JudgeModelOption } from "@/app/lib/evaluators";
import { AuditOptions, AuditRoles, DEFAULT_OPTIONS, FIELD, ModelPicker, Role, configOf, type AuditOptionsValue } from "./AuditOptions";
import { Badge } from "./ui";
import { IconChevron } from "./icons";


export function AuditsManager({
  initial,
  catalog,
  agents,
}: {
  initial: Audit[];
  catalog: AuditCatalog;
  agents: AgentRow[];
}) {
  const [composing, setComposing] = useState(initial.length === 0);

  return (
    <div className="space-y-5">
      <section className="reveal card overflow-hidden">
        <div className="flex items-center justify-between border-b border-line px-4 py-3">
          <span className="text-[13px] font-semibold text-fg">
            Audits <span className="font-mono text-[11px] text-fg-faint">({initial.length})</span>
          </span>
          <button onClick={() => setComposing((c) => !c)} className="btn-ghost text-[12.5px]">
            {composing ? "Cancel" : "+ New audit"}
          </button>
        </div>

        {composing && <NewAuditForm catalog={catalog} agents={agents} />}

        {initial.length === 0 && !composing ? (
          <div className="px-4 py-14 text-center text-[13px] text-fg-faint">No audits yet.</div>
        ) : (
          initial.map((a) => <AuditRow key={a.id} audit={a} />)
        )}
      </section>
    </div>
  );
}

function AuditRow({ audit }: { audit: Audit }) {
  const run = audit.last_run;
  const rate = run?.summary?.elicitation_rate;
  return (
    <Link
      href={`/audits/${audit.id}`}
      className="group grid grid-cols-[1fr_auto] items-center gap-4 border-b border-line/50 px-4 py-3 transition-colors last:border-0 hover:bg-hilite/[0.025]"
    >
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-[13px] text-fg">{audit.name}</span>
          <Badge variant={audit.config.target.control === "fixed" ? "info" : "neutral"}>
            {audit.config.target.control === "fixed" ? "fixed prompt" : "attacker-built"}
          </Badge>
          {audit.config.environment?.mode === "sandbox" && <Badge variant="signal">real machine</Badge>}
        </div>
        <div className="mt-1 truncate font-mono text-[11.5px] text-fg-faint">
          {audit.config.target.model}
          {audit.agent ? ` · from ${audit.agent}` : ""} · {audit.seeds} seed{audit.seeds === 1 ? "" : "s"}
        </div>
        {audit.behavior && (
          <div className="mt-1 truncate text-[12px] text-fg-muted">{audit.behavior}</div>
        )}
      </div>
      <div className="flex items-center gap-4">
        {run ? (
          <div className="text-right">
            <div
              className={clsx(
                "font-display text-[20px] font-bold leading-none",
                // an explore run's rollouts learn from each other: shown, but never as a measured rate
                rate == null || run.mode === "explore" ? "text-fg-faint" : rate > 0 ? "text-fail" : "text-ok",
              )}
            >
              {run.status === "RUNNING" ? "…" : formatRate(rate)}
            </div>
            <div className="mt-1 font-mono text-[10.5px] uppercase tracking-wide text-fg-faint">
              {run.status === "RUNNING" ? `running ${run.done}/${run.total}` : run.mode === "explore" ? "explore run" : "elicitation"}
            </div>
          </div>
        ) : (
          <span className="font-mono text-[11px] text-fg-faint">never run</span>
        )}
        <IconChevron className="h-4 w-4 text-fg-faint transition-colors group-hover:text-signal" />
      </div>
    </Link>
  );
}

export function parseTools(text: string): TargetTool[] {
  const raw = text.trim();
  if (!raw) return [];
  const parsed: unknown = JSON.parse(raw);
  if (!Array.isArray(parsed)) throw new Error("Tools must be a JSON array.");
  return parsed.map((t) => {
    // Accept both the flat shape and OpenAI's `{type: "function", function: {...}}` wrapper —
    // whichever the user pasted from their own code.
    const fn = (t && typeof t === "object" && "function" in t ? (t as { function: unknown }).function : t) as Partial<TargetTool>;
    if (!fn?.name) throw new Error("Every tool needs a name.");
    return {
      name: String(fn.name),
      description: String(fn.description ?? ""),
      parameters: (fn.parameters as Record<string, unknown>) ?? { type: "object", properties: {} },
    };
  });
}

function NewAuditForm({
  catalog,
  agents,
}: {
  catalog: AuditCatalog;
  agents: AgentRow[];
}) {
  const uid = useId();
  const router = useRouter();
  const [models, setModels] = useState<JudgeModelOption[]>([]);
  const [preset, setPreset] = useState<AuditPreset | null>(null);
  const [name, setName] = useState("");
  const [behavior, setBehavior] = useState("");
  const [control, setControl] = useState<"auditor" | "fixed">("auditor");
  const [model, setModel] = useState("");
  const [agent, setAgent] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [toolsText, setToolsText] = useState("");
  const [opts, setOpts] = useState<AuditOptionsValue>(DEFAULT_OPTIONS);
  const set = (patch: Partial<AuditOptionsValue>) => setOpts((cur) => ({ ...cur, ...patch }));
  const [importNote, setImportNote] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // Decision models classify; none of the three roles here can run on one.
    void listJudgeModels()
      .then((m) => {
        const llms = m.models.filter((x) => x.kind !== "decision");
        setModels(llms);
        const fallback = m.text_default || llms[0]?.id || "";
        setModel((cur) => cur || fallback);
        setOpts((cur) => (cur.judges.length || !fallback ? cur : { ...cur, judges: [fallback] }));
      })
      .catch(() => {});
  }, []);

  function choosePreset(p: AuditPreset | null) {
    setPreset(p);
    setBehavior(p ? p.behaviour : "");
    set({ dims: p ? p.dimensions : [] });
  }

  /** Picking an agent fills in its prompt and tools straight away — there is no second step. */
  async function chooseAgent(slug: string) {
    setAgent(slug);
    setImportNote(null);
    setErr(null);
    if (!slug) return;
    const r = await fetch("/api/audits/import-target", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ agent: slug }),
    });
    const d = await r.json().catch(() => null);
    if (!r.ok) return setErr(d?.detail ?? `Could not read that agent (HTTP ${r.status}).`);
    if (!d.found) {
      return setImportNote("No recent trace of this agent shows a system prompt or tools. Paste them under “Prompt and tools”.");
    }
    setSystemPrompt(d.system_prompt ?? "");
    setToolsText(d.tools?.length ? JSON.stringify(d.tools, null, 2) : "");
    setImportNote(
      `Took the system prompt and ${d.tools?.length ?? 0} tool${d.tools?.length === 1 ? "" : "s"} from its latest trace` +
        (d.model ? ` (it ran on ${d.model}).` : "."),
    );
  }

  async function submit() {
    setErr(null);
    let tools: TargetTool[] = [];
    if (control === "fixed") {
      try {
        tools = parseTools(toolsText);
      } catch (e) {
        return setErr(e instanceof Error ? e.message : "The tools JSON is not valid.");
      }
    }
    setBusy(true);
    try {
      const r = await fetch("/api/audits", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          // A name nobody typed: the preset's, else the first words of the behaviour.
          name: name.trim() || preset?.name || behavior.trim().split(/\s+/).slice(0, 6).join(" "),
          behavior,
          agent: control === "fixed" ? agent : "",
          preset: preset?.key,
          seeds: preset?.seeds ?? [],
          config: {
            target: { model, control, system_prompt: control === "fixed" ? systemPrompt : "", tools },
            ...configOf(opts, catalog),
          },
        }),
      });
      const d = await r.json().catch(() => null);
      if (r.ok && d?.id) return router.push(`/audits/${d.id}`);
      setErr(d?.detail ?? `Could not create the audit (HTTP ${r.status}).`);
    } catch {
      setErr("Could not create the audit: the server is unreachable.");
    } finally {
      setBusy(false);
    }
  }

  const box = opts.sandbox && Boolean(catalog.sandbox?.available);
  const ready = behavior.trim() && model && opts.judges.length > 0 && (control === "auditor" || systemPrompt.trim());

  return (
    <div className="space-y-6 border-b border-line bg-ink-900/40 px-4 py-5">
      {/* ── 1 ── */}
      <div>
        <label className="text-[14px] font-semibold text-fg" htmlFor={`${uid}-behavior`}>
          1. What should the audit look for?
        </label>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {catalog.presets.map((p) => (
            <Chip key={p.key} on={preset?.key === p.key} onClick={() => choosePreset(preset?.key === p.key ? null : p)}
                  title={`Fills in the description and ${p.seeds.length} starter seed${p.seeds.length === 1 ? "" : "s"}`}>
              {p.name}
            </Chip>
          ))}
        </div>
        <textarea id={`${uid}-behavior`} rows={3} className={clsx(FIELD, "mt-2 font-sans")} value={behavior}
                  onChange={(e) => setBehavior(e.target.value)}
                  placeholder="Pick one above, or describe it: The agent grants refunds its policy does not allow when the customer insists." />
      </div>

      {/* ── 2: three roles, one layout ── */}
      <div>
        <div className="text-[14px] font-semibold text-fg">2. Who plays each part?</div>
        <p className="mt-0.5 text-[12px] text-fg-faint">
          An audit is a conversation between two models, read afterwards by one or more others.
        </p>
        <div className="mt-2 divide-y divide-line rounded-lg border border-line">
          <Role name="Model under test" does="The model you are auditing. Test it bare, or as one of your agents with its real prompt and tools.">
            <ModelPicker label="Model under test" models={models} value={model} onChange={setModel} />
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <Chip on={control === "auditor"} onClick={() => setControl("auditor")}
                    title="The attacker writes a system prompt and tools to fit each situation">
                On its own
              </Chip>
              <Chip on={control === "fixed"} onClick={() => setControl("fixed")}
                    title="Your agent's real system prompt and tools are what is tested, unchanged">
                As one of my agents
              </Chip>
              {control === "fixed" && agents.length > 0 && (
                <span className="w-48">
                  <select aria-label="Agent" className={FIELD} value={agent} onChange={(e) => void chooseAgent(e.target.value)}>
                    <option value="">Choose an agent…</option>
                    {agents.map((a) => (
                      <option key={a.id} value={a.slug}>{a.display_name || a.slug}</option>
                    ))}
                  </select>
                </span>
              )}
            </div>
            {importNote && <p className="mt-2 text-[12px] text-fg-muted">{importNote}</p>}
            {control === "fixed" && (
              <details className="mt-2" open={!systemPrompt && !agent}>
                <summary className="cursor-pointer text-[12px] text-fg-faint transition-colors hover:text-signal">
                  Prompt and tools{systemPrompt ? " (filled in — open to edit)" : ""}
                </summary>
                <div className="mt-2 space-y-2">
                  <textarea aria-label="System prompt" rows={5} className={FIELD} value={systemPrompt}
                            onChange={(e) => setSystemPrompt(e.target.value)} placeholder="System prompt — You are a support agent for…" />
                  <textarea aria-label="Tools (JSON array)" rows={5} className={FIELD} value={toolsText}
                            onChange={(e) => setToolsText(e.target.value)}
                            placeholder={'Tools, as a JSON array — [{"name": "lookup_order", "description": "…", "parameters": {"type": "object", "properties": {}}}]'} />
                </div>
              </details>
            )}
          </Role>
          <AuditRoles value={opts} set={set} models={models} target={model} />
        </div>
      </div>

      {/* ── everything else has a default ── */}
      <details className="group rounded-lg border border-line">
        <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2.5 text-[12.5px] text-fg-muted transition-colors hover:text-fg">
          <IconChevron className="h-3.5 w-3.5 text-fg-faint transition-transform group-open:rotate-90" />
          More options
          <span className="truncate font-mono text-[11px] text-fg-faint">
            {box ? "real machine" : "invented tool results"} · {opts.maxTurns} turns · ${opts.budget} per run
          </span>
        </summary>
        <div className="space-y-6 border-t border-line px-3 py-4">
          <div>
            <label className="block text-[13px] font-medium text-fg" htmlFor={`${uid}-name`}>Name</label>
            <input id={`${uid}-name`} className={clsx(FIELD, "mt-2 max-w-sm")} value={name} onChange={(e) => setName(e.target.value)}
                   placeholder={preset?.name || "Named after the behaviour"} />
          </div>
          <AuditOptions value={opts} set={set} catalog={catalog} />
        </div>
      </details>

      <div className="flex items-center justify-end gap-3">
        {err && <p role="alert" className="mr-auto text-[12px] text-fail">{err}</p>}
        <button type="button" onClick={submit} disabled={busy || !ready} className="btn-primary">
          {busy ? "Creating…" : "Create audit"}
        </button>
      </div>
    </div>
  );
}

function Chip({
  on,
  onClick,
  children,
  title,
}: {
  on: boolean;
  onClick: () => void;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={on}
      title={title}
      className={clsx(
        "rounded-md border px-2 py-1 font-mono text-[11.5px] transition-colors",
        on ? "border-signal/50 bg-signal/10 text-signal" : "border-line text-fg-muted hover:border-line-bright",
      )}
    >
      {children}
    </button>
  );
}
