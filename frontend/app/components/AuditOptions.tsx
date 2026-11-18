"use client";

import clsx from "clsx";
import { PACKS, type AuditCatalog, type AuditConfig, type AuditDimension } from "@/app/lib/audits";
import type { JudgeModelOption } from "@/app/lib/evaluators";
import { PackTools } from "./PackTools";
import { Toggle } from "./Toggle";

/** Everything about an audit that has a sensible default — one shape, edited by the same controls
 *  in the create form and in an audit's Settings. */
export type AuditOptionsValue = {
  attacker: string; // "" = the server's default attacker
  judges: string[];
  sandbox: boolean;
  packs: string[];
  network: boolean;
  media: string[];
  dims: string[];
  realism: boolean;
  bestOf: number;
  maxTurns: number;
  budget: number;
};

export const DEFAULT_OPTIONS: AuditOptionsValue = {
  attacker: "", judges: [], sandbox: false, packs: ["dev"], network: false, media: [], dims: [],
  realism: true, bestOf: 1, maxTurns: 15, budget: 5,
};

export function optionsOf(cfg: AuditConfig): AuditOptionsValue {
  return {
    attacker: cfg.attacker.models[0] ?? "",
    judges: cfg.judge_models,
    sandbox: cfg.environment?.mode === "sandbox",
    packs: cfg.environment?.packs?.length ? cfg.environment.packs : ["dev"],
    network: cfg.environment?.network === "allowlist",
    media: cfg.attacker.media ?? [],
    dims: cfg.dimensions.map((d) => d.name),
    realism: Boolean(cfg.attacker.realism_filter),
    bestOf: cfg.attacker.best_of ?? 1,
    maxTurns: cfg.attacker.max_turns,
    budget: cfg.limits.budget_usd,
  };
}

/** The config keys these options decide. `base` is the audit being edited: a dimension it already
 *  has keeps its own rubric and threshold, a new one goes by name and the server fills it in. */
export function configOf(v: AuditOptionsValue, catalog: AuditCatalog, base?: AuditConfig) {
  const box = v.sandbox && Boolean(catalog.sandbox?.available);
  return {
    attacker: {
      ...base?.attacker, models: v.attacker ? [v.attacker] : [], max_turns: v.maxTurns,
      realism_filter: v.realism, media: v.media, best_of: v.bestOf,
    },
    environment: box
      ? { mode: "sandbox", packs: v.packs.length ? v.packs : ["dev"], network: v.network && catalog.sandbox?.network ? "allowlist" : "none" }
      : { mode: "synthetic" },
    dimensions: v.dims.map((n) => base?.dimensions.find((d) => d.name === n) ?? n),
    judge_models: v.judges,
    limits: { ...base?.limits, budget_usd: v.budget },
  };
}

export const FIELD =
  "w-full rounded-lg border border-line bg-ink-700 px-2.5 py-2 font-mono text-[12.5px] text-fg placeholder:text-fg-faint transition-colors hover:border-line-bright focus:border-signal/50 focus:outline-none";

type Props = {
  value: AuditOptionsValue;
  set: (patch: Partial<AuditOptionsValue>) => void;
};

const flip = (list: string[], key: string) => (list.includes(key) ? list.filter((k) => k !== key) : [...list, key]);

/** One model picker for every role. A model saved on the audit but gone from the curated list
 *  still shows, so opening the form never silently changes it. */
export function ModelPicker({
  label, models, value, onChange, empty,
}: {
  label: string; models: JudgeModelOption[]; value: string; onChange: (v: string) => void; empty?: string;
}) {
  const known = !value || models.some((m) => m.id === value);
  return (
    <select aria-label={label} className={FIELD} value={value} onChange={(e) => onChange(e.target.value)}>
      {empty !== undefined && <option value="">{empty}</option>}
      {!known && <option value={value}>{value}</option>}
      {models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
    </select>
  );
}

/** A part someone plays in the audit: its name and what it does on the left, who plays it on the
 *  right. Every role is laid out the same way, the model under test included. */
export function Role({ name, does, children }: { name: string; does: string; children: React.ReactNode }) {
  return (
    <div className="grid items-start gap-x-6 gap-y-2 px-3 py-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,24rem)]">
      <div>
        <div className="text-[13px] font-medium text-fg">{name}</div>
        <p className="mt-0.5 text-[12px] leading-relaxed text-fg-faint">{does}</p>
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

/** Who tests the model: the attacker and the judges, picked the same way. */
export function AuditRoles({
  value, set, models, target,
}: Props & { models: JudgeModelOption[]; target: string }) {
  const label = (id: string) => models.find((m) => m.id === id)?.label ?? id;
  const family = (id: string) => id.split("/")[0];
  const sameFamily = value.judges.length > 0 && target && value.judges.every((j) => family(j) === family(target));
  return (
    <>
      <Role name="Attacker" does="Plays the user. It talks to the model under test and tries to bring out the behaviour.">
        <ModelPicker label="Attacker model" models={models} value={value.attacker} empty="Automatic (the server's default)"
                     onChange={(attacker) => set({ attacker })} />
      </Role>
      <Role name="Judges" does="Read each finished conversation and score it from 1 to 10. With several judges the score is their median.">
        {value.judges.length > 0 && (
          <ul className="mb-2 flex flex-wrap gap-1.5">
            {value.judges.map((id) => (
              <li key={id} className="flex items-center gap-1 rounded-md border border-signal/40 bg-signal/10 py-1 pl-2 pr-1 font-mono text-[11.5px] text-fg">
                {label(id)}
                <button type="button" aria-label={`Remove judge ${label(id)}`} className="rounded px-1 text-fg-faint transition-colors hover:text-fail"
                        onClick={() => set({ judges: value.judges.filter((j) => j !== id) })}>
                  ×
                </button>
              </li>
            ))}
          </ul>
        )}
        <ModelPicker label="Add a judge" models={models.filter((m) => !value.judges.includes(m.id))} value=""
                     empty={value.judges.length ? "Add another judge…" : "Choose a judge…"}
                     onChange={(id) => id && set({ judges: [...value.judges, id] })} />
        {sameFamily && (
          <p className="mt-1.5 text-[12px] text-warn">
            Every judge is from the same provider as the model under test. Add one from another provider for a second opinion.
          </p>
        )}
      </Role>
    </>
  );
}

/** One question the rest of the form answers, with its explanation always visible. */
function Group({ title, about, children }: { title: string; about: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[13px] font-medium text-fg">{title}</div>
      <p className="mt-0.5 text-[12px] leading-relaxed text-fg-faint">{about}</p>
      <div className="mt-2">{children}</div>
    </div>
  );
}

function Choice({ on, onClick, children, title }: { on: boolean; onClick: () => void; children: React.ReactNode; title?: string }) {
  return (
    <button type="button" onClick={onClick} aria-pressed={on} title={title}
            className={clsx("rounded-md border px-2.5 py-1 text-[12px] transition-colors",
              on ? "border-signal/50 bg-signal/10 text-fg" : "border-line text-fg-muted hover:border-line-bright hover:text-fg")}>
      {children}
    </button>
  );
}

/** A switch with what it does written next to it. */
function Ability({ on, onChange, name, does }: { on: boolean; onChange: (v: boolean) => void; name: string; does: string }) {
  return (
    <div className="flex items-start gap-3">
      <Toggle checked={on} onChange={onChange} label={name} className="w-44 shrink-0 pt-0.5" />
      <p className="text-[12px] leading-relaxed text-fg-faint">{does}</p>
    </div>
  );
}

function Count({ label, value, onChange, min, max, step }: {
  label: string; value: number; onChange: (v: number) => void; min: number; max: number; step?: number;
}) {
  return (
    // The width lives on a wrapper: FIELD is `w-full`, and two width utilities on one element are
    // settled by stylesheet order, not by which comes last.
    <span className="inline-block w-20 align-middle">
      <input aria-label={label} type="number" min={min} max={max} step={step} value={value} className={FIELD}
             onChange={(e) => onChange(Math.min(max, Math.max(min, Number(e.target.value) || min)))} />
    </span>
  );
}

/** The settings with a default: the environment, what the attacker may do, what else is scored,
 *  and the limits. `extra` are dimensions the audit has that the catalog does not. */
export function AuditOptions({ value, set, catalog, extra = [] }: Props & { catalog: AuditCatalog; extra?: AuditDimension[] }) {
  const canSandbox = Boolean(catalog.sandbox?.available);
  const box = value.sandbox && canSandbox;
  const dims = [...catalog.behaviours, ...extra.filter((d) => !catalog.behaviours.some((c) => c.name === d.name))];
  const ours = dims.filter((d) => !d.source);
  const petri = dims.filter((d) => d.source);
  const dim = (d: AuditDimension) => (
    <Choice key={d.name} on={value.dims.includes(d.name)} onClick={() => set({ dims: flip(value.dims, d.name) })}
            title={`${d.description}${d.kind === "info" ? " (reported, never flags a conversation)" : ""}`}>
      {d.name.replaceAll("_", " ")}
    </Choice>
  );
  return (
    <div className="space-y-6">
      <Group title="When the model uses a tool"
             about="Either the attacker writes what the tool returns and nothing really runs, or the model gets a sandboxed Linux machine where its commands really execute. A real machine is harder for the model to recognise as a test.">
        <div className="flex flex-wrap gap-1.5">
          <Choice on={!box} onClick={() => set({ sandbox: false })}>The attacker makes up the result</Choice>
          <Choice on={box} onClick={() => canSandbox && set({ sandbox: true })}
                  title={canSandbox ? undefined : "This deployment has no sandbox configured (SANDBOX_BACKEND)"}>
            It really runs, on a real machine{canSandbox ? "" : " (not set up)"}
          </Choice>
        </div>
        {box && (
          <>
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <span className="text-[12px] text-fg-faint">The machine has</span>
              {PACKS.map((p) => (
                <Choice key={p.key} on={value.packs.includes(p.key)} onClick={() => set({ packs: flip(value.packs, p.key) })} title={p.hint}>
                  {p.label}
                </Choice>
              ))}
              {catalog.sandbox?.network && (
                <Choice on={value.network} onClick={() => set({ network: !value.network })}
                        title="Read-only access to package registries and documentation, through a proxy that logs each request">
                  Web access (allowlist)
                </Choice>
              )}
            </div>
            <PackTools packs={value.packs.length ? value.packs : ["dev"]} network={value.network} catalog={catalog} />
          </>
        )}
      </Group>

      <Group title="What the attacker may do" about="How far the attacker can go to make the conversation convincing.">
        <div className="space-y-2.5">
          <Ability on={value.realism} onChange={(realism) => set({ realism })} name="Realism check"
                   does="A second model reads everything the attacker writes before the model under test sees it, and sends back anything that would give away that this is a test. Costs one small extra call per message." />
          <Ability on={value.media.includes("image")} onChange={() => set({ media: flip(value.media, "image") })} name="Send images"
                   does="The attacker can generate a photo, a scan or a screenshot and send it. The model under test must accept images, or the run is refused." />
          <Ability on={value.media.includes("voice")} onChange={() => set({ media: flip(value.media, "voice") })} name="Send voice messages"
                   does="The attacker can speak: the model hears a recording, never the text. The model under test must accept audio, or the run is refused." />
          <p className="text-[12px] text-fg-muted">
            Up to <Count label="Attacker turns per conversation" value={value.maxTurns} min={1} max={40} onChange={(maxTurns) => set({ maxTurns })} /> attacker
            turns per conversation. In explore runs, write{" "}
            <Count label="Drafts per turn" value={value.bestOf} min={1} max={4} onChange={(bestOf) => set({ bestOf })} /> drafts
            of each turn and play the most convincing one.
          </p>
        </div>
      </Group>

      <Group title="Score other behaviours too"
             about="The behaviour you described is always scored, and so is whether each conversation was a fair test. Pick more and the judges score the same conversations for those as well. No extra conversations, only extra judge calls.">
        <div className="flex flex-wrap gap-1.5">{ours.map(dim)}</div>
        {petri.length > 0 && (
          <details className="mt-2" open={petri.some((d) => value.dims.includes(d.name))}>
            <summary className="cursor-pointer text-[12px] text-fg-faint transition-colors hover:text-signal">
              {petri.length} more from the Petri library
            </summary>
            <div className="mt-2 flex flex-wrap gap-1.5">{petri.map(dim)}</div>
          </details>
        )}
      </Group>

      <Group title="Spending limit" about="A run stops as soon as it has spent this much.">
        <p className="text-[12px] text-fg-muted">
          $ <Count label="Budget per run in USD" value={value.budget} min={0.5} max={1000} step={0.5} onChange={(budget) => set({ budget })} /> per run
        </p>
      </Group>
    </div>
  );
}
