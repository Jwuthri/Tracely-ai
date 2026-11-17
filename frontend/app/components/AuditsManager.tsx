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

        {composing && <NewAuditForm catalog={catalog} agents={agents} onCancel={() => setComposing(false)} />}

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
  onCancel,
}: {
  catalog: AuditCatalog;
  agents: AgentRow[];
  onCancel: () => void;
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
    if (p) {
      setName((cur) => cur || p.name);
      setBehavior(p.behaviour);
      setDims(p.dimensions);
    }
  }

  async function importTarget() {
    if (!agent) return;
    setImportNote(null);
    setErr(null);
    const r = await fetch("/api/audits/import-target", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ agent }),
    });
    const d = await r.json().catch(() => null);
    if (!r.ok) return setErr(d?.detail ?? `Could not read that agent (HTTP ${r.status}).`);
    if (!d.found) {
      return setImportNote(
        "No recent trace of this agent shows a system prompt or tools. Paste them below instead.",
      );
    }
    setSystemPrompt(d.system_prompt ?? "");
    setToolsText(d.tools?.length ? JSON.stringify(d.tools, null, 2) : "");
    setImportNote(
      `Imported ${d.tools?.length ?? 0} tool${d.tools?.length === 1 ? "" : "s"} and the system prompt from a recent trace` +
        (d.model ? ` (it ran on ${d.model} — pick the model to audit below).` : "."),
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
          name: name.trim(),
          behavior: behavior.trim(),
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
            attacker: { models: attacker ? [attacker] : [], max_turns: maxTurns },
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

  return (
    <div className="space-y-6 border-b border-line bg-ink-900/40 px-4 py-5">
      <div>
        <div className={LABEL}>Start from</div>
        <div className="mt-2 grid gap-2 sm:grid-cols-3">
          {catalog.presets.map((p) => (
            <button
              key={p.key}
              type="button"
              onClick={() => choosePreset(preset?.key === p.key ? null : p)}
              aria-pressed={preset?.key === p.key}
              className={clsx(
                "rounded-lg border px-3 py-2.5 text-left transition-colors",
                preset?.key === p.key
                  ? "border-signal/50 bg-signal/10"
                  : "border-line hover:border-line-bright",
              )}
            >
              <div className="text-[13px] text-fg">{p.name}</div>
              <div className="mt-1 font-mono text-[10.5px] text-fg-faint">
                {p.seeds.length} starter seed{p.seeds.length === 1 ? "" : "s"}
              </div>
            </button>
          ))}
        </div>
        <p className={HINT}>
          A preset fills in the behaviour, its judging dimensions and a few seeds. Leave them all
          unselected to describe your own.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-[1fr_2fr]">
        <div>
          <label className={LABEL} htmlFor={`${uid}-name`}>Name</label>
          <input id={`${uid}-name`} className={clsx(FIELD, "mt-1")} value={name}
                 onChange={(e) => setName(e.target.value)} placeholder="Refund policy pressure" />
        </div>
        <div>
          <label className={LABEL} htmlFor={`${uid}-behavior`}>Behaviour to look for</label>
          <textarea id={`${uid}-behavior`} rows={3} className={clsx(FIELD, "mt-1 font-sans")} value={behavior}
                    onChange={(e) => setBehavior(e.target.value)}
                    placeholder="The agent grants refunds its policy does not allow when the customer insists." />
          <p className={HINT}>
            Told to the attacker (so it knows what to provoke) and to the judges (so they know what
            counts). Never shown to the target.
          </p>
        </div>
      </div>

      <fieldset>
        <legend className={LABEL}>Target</legend>
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          {([
            ["auditor", "A model", "The attacker writes the system prompt and invents the tools for each seed. For auditing a model itself."],
            ["fixed", "My agent's prompt and tools", "The system prompt and tools below are what is tested, unchanged. For auditing a deployed agent."],
          ] as const).map(([value, title, text]) => (
            <button key={value} type="button" onClick={() => setControl(value)} aria-pressed={control === value}
                    className={clsx("rounded-lg border px-3 py-2.5 text-left transition-colors",
                      control === value ? "border-signal/50 bg-signal/10" : "border-line hover:border-line-bright")}>
              <div className="text-[13px] text-fg">{title}</div>
              <div className="mt-1 text-[12px] leading-relaxed text-fg-faint">{text}</div>
            </button>
          ))}
        </div>

        <div className="mt-3 grid gap-4 sm:grid-cols-2">
          <div>
            <label className={LABEL} htmlFor={`${uid}-model`}>Model under test</label>
            <ModelSelect id={`${uid}-model`} models={models} value={model} onChange={setModel} />
          </div>
          {control === "fixed" && (
            <div>
              <label className={LABEL} htmlFor={`${uid}-agent`}>Import from an agent&apos;s traces</label>
              <div className="mt-1 flex gap-2">
                <select id={`${uid}-agent`} className={FIELD} value={agent} onChange={(e) => setAgent(e.target.value)}>
                  <option value="">Choose an agent…</option>
                  {agents.map((a) => (
                    <option key={a.id} value={a.slug}>{a.display_name || a.slug}</option>
                  ))}
                </select>
                <button type="button" onClick={importTarget} disabled={!agent} className="btn-ghost shrink-0">
                  Import
                </button>
              </div>
            </div>
          )}
        </div>
        {importNote && <p className="mt-2 text-[12px] text-fg-muted">{importNote}</p>}

        {control === "fixed" && (
          <div className="mt-3 grid gap-4 sm:grid-cols-2">
            <div>
              <label className={LABEL} htmlFor={`${uid}-sys`}>System prompt</label>
              <textarea id={`${uid}-sys`} rows={8} className={clsx(FIELD, "mt-1")} value={systemPrompt}
                        onChange={(e) => setSystemPrompt(e.target.value)} placeholder="You are a support agent for…" />
            </div>
            <div>
              <label className={LABEL} htmlFor={`${uid}-tools`}>Tools (JSON array)</label>
              <textarea id={`${uid}-tools`} rows={8} className={clsx(FIELD, "mt-1")} value={toolsText}
                        onChange={(e) => setToolsText(e.target.value)}
                        placeholder={'[{"name": "lookup_order", "description": "…", "parameters": {"type": "object", "properties": {}}}]'} />
              <p className={HINT}>
                Nothing here is ever called. The attacker answers each tool call with the result the
                real tool would return.
              </p>
            </div>
          </div>
        )}
      </fieldset>

      <fieldset>
        <legend className={LABEL}>Judges</legend>
        <p className={HINT}>
          Each selected model scores every dimension on its own; the panel&apos;s answer is the
          median, and a split panel is asked again before it is called.
        </p>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {models.map((m) => (
            <Chip key={m.id} on={judges.includes(m.id)} onClick={() => toggle(judges, setJudges, m.id)}>
              {m.label}
            </Chip>
          ))}
        </div>
        {sameFamily && (
          <p className="mt-2 text-[12px] text-warn">
            Every judge is from the same provider as the model under test. Judges tend to favour
            their own family — add one from another provider.
          </p>
        )}

        <div className={clsx(LABEL, "mt-4")}>Dimensions</div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {catalog.behaviours.map((d) => (
            <Chip key={d.name} on={dims.includes(d.name)} onClick={() => toggle(dims, setDims, d.name)} title={d.description}>
              {d.name.replaceAll("_", " ")}
            </Chip>
          ))}
        </div>
        <p className={HINT}>
          {dims.length === 0
            ? "None selected: judges score the behaviour described above. "
            : ""}
          Three more are always judged — whether the target noticed the test, whether the scenario
          was realistic, and whether the attacker did its job. A rollout that fails one of those is
          excluded from the result instead of counted.
        </p>
      </fieldset>

      <div className="grid gap-4 sm:grid-cols-3">
        <div>
          <label className={LABEL} htmlFor={`${uid}-attacker`}>Attacker model</label>
          <ModelSelect id={`${uid}-attacker`} models={models} value={attacker} onChange={setAttacker} allowDefault />
        </div>
        <div>
          <label className={LABEL} htmlFor={`${uid}-turns`}>Attacker turns per rollout</label>
          <input id={`${uid}-turns`} type="number" min={1} max={40} className={clsx(FIELD, "mt-1")} value={maxTurns}
                 onChange={(e) => setMaxTurns(Number(e.target.value) || 1)} />
        </div>
        <div>
          <label className={LABEL} htmlFor={`${uid}-budget`}>Budget per run (USD)</label>
          <input id={`${uid}-budget`} type="number" min={0.5} step={0.5} className={clsx(FIELD, "mt-1")} value={budget}
                 onChange={(e) => setBudget(Number(e.target.value) || 1)} />
        </div>
      </div>

      <div className="flex items-center justify-end gap-3">
        {err && <p role="alert" className="mr-auto text-[12px] text-fail">{err}</p>}
        <button type="button" onClick={onCancel} className="btn-ghost">Cancel</button>
        <button type="button" onClick={submit} disabled={busy || !name.trim() || !model || judges.length === 0}
                className="btn-primary">
          {busy ? "Creating…" : "Create audit"}
        </button>
      </div>
    </div>
  );
}

function ModelSelect({
  id,
  models,
  value,
  onChange,
  allowDefault = false,
}: {
  id: string;
  models: JudgeModelOption[];
  value: string;
  onChange: (v: string) => void;
  allowDefault?: boolean;
}) {
  // A model saved on the audit but no longer in the curated list still has to show as selected.
  const known = models.some((m) => m.id === value);
  return (
    <select id={id} className={clsx(FIELD, "mt-1")} value={value} onChange={(e) => onChange(e.target.value)}>
      {allowDefault && <option value="">Server default</option>}
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
