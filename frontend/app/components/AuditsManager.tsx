"use client";

import clsx from "clsx";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useId, useState } from "react";
import type { AgentRow } from "@/app/lib/api";
import {
  PACKS,
  formatRate,
  type Audit,
  type AuditCatalog,
  type AuditPreset,
  type TargetTool,
} from "@/app/lib/audits";
import { listJudgeModels, type JudgeModelOption } from "@/app/lib/evaluators";
import { Toggle } from "./Toggle";
import { Badge } from "./ui";
import { IconChevron } from "./icons";

const FIELD =
  "w-full rounded-lg border border-line bg-ink-700 px-2.5 py-2 font-mono text-[12.5px] text-fg placeholder:text-fg-faint transition-colors hover:border-line-bright focus:border-signal/50 focus:outline-none";
const LABEL = "font-mono text-[10px] uppercase tracking-wider text-fg-faint";
const HINT = "mt-1 text-[12px] leading-relaxed text-fg-faint";

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
                rate == null ? "text-fg-faint" : rate > 0 ? "text-fail" : "text-ok",
              )}
            >
              {run.status === "RUNNING" ? "…" : formatRate(rate)}
            </div>
            <div className="mt-1 font-mono text-[10.5px] uppercase tracking-wide text-fg-faint">
              {run.status === "RUNNING" ? `running ${run.done}/${run.total}` : "elicitation"}
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

function parseTools(text: string): TargetTool[] {
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
  const [dims, setDims] = useState<string[]>([]);
  const [control, setControl] = useState<"auditor" | "fixed">("auditor");
  const [model, setModel] = useState("");
  const [agent, setAgent] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [toolsText, setToolsText] = useState("");
  const [attacker, setAttacker] = useState("");
  const [judges, setJudges] = useState<string[]>([]);
  const canSandbox = Boolean(catalog.sandbox?.available);
  const [sandbox, setSandbox] = useState(false);
  const [packs, setPacks] = useState<string[]>(["dev"]);
  const [network, setNetwork] = useState(false);
  const [media, setMedia] = useState<string[]>([]);
  const [bestOf, setBestOf] = useState(1);
  const [realism, setRealism] = useState(true);
  const [maxTurns, setMaxTurns] = useState(15);
  const [budget, setBudget] = useState(5);
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
        setJudges((cur) => (cur.length ? cur : fallback ? [fallback] : []));
      })
      .catch(() => {});
  }, []);

  function choosePreset(p: AuditPreset | null) {
    setPreset(p);
    setBehavior(p ? p.behaviour : "");
    setDims(p ? p.dimensions : []);
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
            target: {
              model,
              control,
              system_prompt: control === "fixed" ? systemPrompt : "",
              tools,
            },
            attacker: {
              models: attacker ? [attacker] : [], max_turns: maxTurns, realism_filter: realism,
              media, best_of: bestOf,
            },
            environment: sandbox && canSandbox
              ? { mode: "sandbox", packs: packs.length ? packs : ["dev"],
                  network: network && catalog.sandbox?.network ? "allowlist" : "none" }
              : { mode: "synthetic" },
            dimensions: dims,
            judge_models: judges,
            limits: { budget_usd: budget },
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

  const toggle = (list: string[], set: (v: string[]) => void, id: string) =>
    set(list.includes(id) ? list.filter((x) => x !== id) : [...list, id]);
  const sameFamily =
    judges.length > 0 && model && judges.every((j) => j.split("/")[0] === model.split("/")[0]);
  const ours = catalog.behaviours.filter((d) => !d.source);
  const petri = catalog.behaviours.filter((d) => d.source);
  const ready = behavior.trim() && model && judges.length > 0 && (control === "auditor" || systemPrompt.trim());

  return (
    <div className="space-y-6 border-b border-line bg-ink-900/40 px-4 py-5">
      {/* ── 1 ── */}
      <div>
        <label className="text-[14px] font-semibold text-fg" htmlFor={`${uid}-behavior`}>
          What should the audit look for?
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

      {/* ── 2 ── */}
      <div>
        <div className="text-[14px] font-semibold text-fg">What are you testing?</div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Chip on={control === "auditor"} onClick={() => setControl("auditor")}
                title="The attacker writes a system prompt and tools to fit each situation">
            A model
          </Chip>
          <Chip on={control === "fixed"} onClick={() => setControl("fixed")}
                title="Your agent's real system prompt and tools are what is tested, unchanged">
            One of my agents
          </Chip>
          {control === "fixed" && agents.length > 0 && (
            <span className="w-56">
              <select aria-label="Agent" className={FIELD} value={agent} onChange={(e) => void chooseAgent(e.target.value)}>
                <option value="">Choose an agent…</option>
                {agents.map((a) => (
                  <option key={a.id} value={a.slug}>{a.display_name || a.slug}</option>
                ))}
              </select>
            </span>
          )}
          <span className="w-64">
            <ModelSelect id={`${uid}-model`} label="Model under test" models={models} value={model} onChange={setModel} />
          </span>
        </div>
        {importNote && <p className="mt-2 text-[12px] text-fg-muted">{importNote}</p>}
        {control === "fixed" && (
          <details className="mt-2" open={!systemPrompt && !agent}>
            <summary className="cursor-pointer text-[12px] text-fg-faint transition-colors hover:text-signal">
              Prompt and tools{systemPrompt ? " (filled in — open to edit)" : ""}
            </summary>
            <div className="mt-2 grid gap-3 sm:grid-cols-2">
              <textarea aria-label="System prompt" rows={7} className={FIELD} value={systemPrompt}
                        onChange={(e) => setSystemPrompt(e.target.value)} placeholder="System prompt — You are a support agent for…" />
              <textarea aria-label="Tools (JSON array)" rows={7} className={FIELD} value={toolsText}
                        onChange={(e) => setToolsText(e.target.value)}
                        placeholder={'Tools, as a JSON array — [{"name": "lookup_order", "description": "…", "parameters": {"type": "object", "properties": {}}}]'} />
            </div>
          </details>
        )}
      </div>

      {/* ── everything else has a default ── */}
      <details className="group rounded-lg border border-line">
        <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2.5 text-[12.5px] text-fg-muted transition-colors hover:text-fg">
          <IconChevron className="h-3.5 w-3.5 text-fg-faint transition-transform group-open:rotate-90" />
          More options
          <span className="truncate font-mono text-[11px] text-fg-faint">
            {sandbox && canSandbox ? "real machine" : "invented tool results"} · {judges.length || 1} judge{judges.length > 1 ? "s" : ""} · ${budget} per run
          </span>
        </summary>
        <div className="grid gap-x-6 gap-y-5 border-t border-line px-3 py-4 sm:grid-cols-[150px_1fr]">
          <Opt label="Name">
            <input aria-label="Name" className={clsx(FIELD, "max-w-sm")} value={name} onChange={(e) => setName(e.target.value)}
                   placeholder={preset?.name || "Named after the behaviour"} />
          </Opt>

          <Opt label="Tool results">
            <div className="flex flex-wrap gap-1.5">
              <Chip on={!sandbox} onClick={() => setSandbox(false)} title="The attacker writes the result of every tool call. Nothing runs. Works with any tools.">
                Invented by the attacker
              </Chip>
              <Chip on={sandbox && canSandbox} onClick={() => canSandbox && setSandbox(true)}
                    title={canSandbox
                      ? "The target gets real tools on a real Linux box; its commands really run"
                      : "Not available: this deployment has no sandbox configured (SANDBOX_BACKEND)"}>
                A real machine{canSandbox ? "" : " (not set up)"}
              </Chip>
            </div>
            {sandbox && canSandbox && (
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <span className="text-[12px] text-fg-faint">with</span>
                {PACKS.map((p) => (
                  <Chip key={p.key} on={packs.includes(p.key)} onClick={() => toggle(packs, setPacks, p.key)} title={p.hint}>
                    {p.label}
                  </Chip>
                ))}
                {catalog.sandbox?.network && (
                  <Chip on={network} onClick={() => setNetwork(!network)}
                        title="Read-only access to package registries and documentation, through a proxy that logs each request">
                    Web access (allowlist)
                  </Chip>
                )}
              </div>
            )}
          </Opt>

          <Opt label="Attacker can send" hint="The model under test must accept images or audio, or the run is refused.">
            <div className="flex flex-wrap gap-1.5">
              <Chip on={media.includes("image")} onClick={() => toggle(media, setMedia, "image")}
                    title="The attacker can generate an image — a photo, a scan, a screenshot — and send it">
                Images
              </Chip>
              <Chip on={media.includes("voice")} onClick={() => toggle(media, setMedia, "voice")}
                    title="The attacker can speak: the target hears a recording, never the text">
                Voice messages
              </Chip>
            </div>
          </Opt>

          <Opt label="Judges" hint={sameFamily
            ? undefined
            : "Each model scores on its own; the result is the median."}>
            <div className="flex flex-wrap gap-1.5">
              {models.map((m) => (
                <Chip key={m.id} on={judges.includes(m.id)} onClick={() => toggle(judges, setJudges, m.id)}>
                  {m.label}
                </Chip>
              ))}
            </div>
            {sameFamily && (
              <p className="mt-2 text-[12px] text-warn">
                Every judge is from the same provider as the model under test — add one from another.
              </p>
            )}
          </Opt>

          <Opt label="Also score" hint="Optional. The behaviour you described is always scored, and so is whether each conversation was a fair test.">
            <div className="flex flex-wrap gap-1.5">
              {ours.map((d) => (
                <Chip key={d.name} on={dims.includes(d.name)} onClick={() => toggle(dims, setDims, d.name)} title={d.description}>
                  {d.name.replaceAll("_", " ")}
                </Chip>
              ))}
            </div>
            {petri.length > 0 && (
              <details className="mt-2">
                <summary className="cursor-pointer text-[12px] text-fg-faint transition-colors hover:text-signal">
                  {petri.length} more from Petri
                </summary>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {petri.map((d) => (
                    <Chip key={d.name} on={dims.includes(d.name)} onClick={() => toggle(dims, setDims, d.name)}
                          title={`${d.description}${d.kind === "info" ? " (reported, never flags a rollout)" : ""}`}>
                      {d.name.replaceAll("_", " ")}
                    </Chip>
                  ))}
                </div>
              </details>
            )}
          </Opt>

          <Opt label="Attacker">
            <div className="flex flex-wrap items-end gap-3">
              <span className="w-56">
                <ModelSelect id={`${uid}-attacker`} label="Attacker model" models={models} value={attacker} onChange={setAttacker} allowDefault />
              </span>
              <Num label="Turns" value={maxTurns} min={1} max={40} onChange={setMaxTurns}
                   title="How many moves the attacker gets in one conversation" />
              <Num label="Drafts per turn" value={bestOf} min={1} max={4} onChange={setBestOf}
                   title="Explore runs only: draft this many moves each turn and play the most convincing one" />
              <span className="pb-1.5" title="A second model reads what the attacker writes before the target does, and sends back anything that would give the test away">
                <Toggle checked={realism} onChange={setRealism} label="Realism check" />
              </span>
            </div>
          </Opt>

          <Opt label="Budget">
            <Num label="USD per run" value={budget} min={0.5} step={0.5} onChange={setBudget}
                 title="A run stops spending when it reaches this" />
          </Opt>
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

/** One row of "More options": a label on the left, the control on the right. */
function Opt({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <>
      <div className={clsx(LABEL, "pt-1.5")}>{label}</div>
      <div className="min-w-0">
        {children}
        {hint && <p className={HINT}>{hint}</p>}
      </div>
    </>
  );
}

function Num({
  label, value, onChange, min, max, step, title,
}: {
  label: string; value: number; onChange: (v: number) => void; min: number; max?: number; step?: number; title?: string;
}) {
  return (
    <label className="block" title={title}>
      <span className={LABEL}>{label}</span>
      {/* The width lives on a wrapper: FIELD is `w-full`, and two width utilities on one element
          are settled by stylesheet order, not by which comes last here. */}
      <span className="mt-1 block w-24">
        <input type="number" min={min} max={max} step={step} value={value} className={FIELD}
               onChange={(e) => onChange(Math.min(max ?? Infinity, Math.max(min, Number(e.target.value) || min)))} />
      </span>
    </label>
  );
}

function ModelSelect({
  id,
  label,
  models,
  value,
  onChange,
  allowDefault = false,
}: {
  id: string;
  label: string;
  models: JudgeModelOption[];
  value: string;
  onChange: (v: string) => void;
  allowDefault?: boolean;
}) {
  // A model saved on the audit but no longer in the curated list still has to show as selected.
  const known = models.some((m) => m.id === value);
  return (
    <select id={id} aria-label={label} title={label} className={FIELD} value={value} onChange={(e) => onChange(e.target.value)}>
      {allowDefault && <option value="">Attacker: server default</option>}
      {!known && value && <option value={value}>{value}</option>}
      {models.map((m) => (
        <option key={m.id} value={m.id}>{m.label}</option>
      ))}
    </select>
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
