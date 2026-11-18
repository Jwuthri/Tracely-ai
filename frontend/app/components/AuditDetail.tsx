"use client";

import clsx from "clsx";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useId, useRef, useState } from "react";
import {
  PACKS,
  fileBase64,
  formatRate,
  formatUsd,
  runProgress,
  type AuditCatalog,
  type AuditDetail as Audit,
  type AuditRun,
  type AuditSeed,
} from "@/app/lib/audits";
import { listJudgeModels, type JudgeModelOption } from "@/app/lib/evaluators";
import { parseTools } from "./AuditsManager";
import { PackTools } from "./PackTools";
import { TimeAgo } from "./TimeAgo";
import { Toggle } from "./Toggle";
import { Badge } from "./ui";
import { IconChevron, IconPlay, IconTrash } from "./icons";

const FIELD =
  "w-full rounded-lg border border-line bg-ink-700 px-2.5 py-2 font-mono text-[12.5px] text-fg placeholder:text-fg-faint transition-colors hover:border-line-bright focus:border-signal/50 focus:outline-none";
const LABEL = "font-mono text-[10px] uppercase tracking-wider text-fg-faint";
const LINK = "text-[12px] text-fg-faint transition-colors hover:text-signal";
const SHOWN = 8; // seeds listed before "Show all"

type Panel = "generate" | "import" | "write" | null;

async function detailOf(r: Response, fallback: string): Promise<string> {
  const d = await r.json().catch(() => null);
  return d?.detail ?? `${fallback} (HTTP ${r.status}).`;
}

/** One audit: run it, read the last result, manage its seeds. Everything that is set once and
 *  rarely looked at again (target, judges, budget, what is judged) sits under Settings. */
export function AuditDetail({ audit, catalog }: { audit: Audit; catalog: AuditCatalog }) {
  const router = useRouter();
  const uid = useId();
  const [seeds, setSeeds] = useState(audit.seeds);
  const [reps, setReps] = useState(audit.config.limits.reps);
  const [mode, setMode] = useState<"measure" | "explore">("measure");
  const [options, setOptions] = useState(false);
  const [panel, setPanel] = useState<Panel>(null);
  const [open, setOpen] = useState<string | null>(null); // the expanded seed
  const [all, setAll] = useState(false);
  const [working, setWorking] = useState<string | null>(null); // which seed tool is busy
  const [guidance, setGuidance] = useState("");
  const [tag, setTag] = useState("");
  const [axes, setAxes] = useState<string[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const settings = useRef<HTMLDetailsElement>(null);

  const enabled = seeds.filter((s) => s.enabled).length;
  const tags = catalog.library?.petri.tags ?? [];
  const variations = catalog.variations ?? [];
  const last = audit.runs.find((r) => r.mode === "measure" && r.status === "DONE") ?? audit.runs[0];
  const rate = last?.summary?.elicitation_rate;

  async function run() {
    setBusy(true);
    setErr(null);
    try {
      const r = await fetch(`/api/audits/${audit.id}/runs`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ reps, mode }),
      });
      if (r.ok) {
        const d = await r.json();
        return router.push(`/audits/${audit.id}/runs/${d.id}`);
      }
      setErr(await detailOf(r, "Could not start the run"));
    } catch {
      setErr("Could not start the run: the server is unreachable.");
    } finally {
      setBusy(false);
    }
  }

  /** Run one of the seed tools and fold what it returns into the list. */
  async function seedTool(key: string, url: string, body: unknown, fallback: string, replace = false) {
    setWorking(key);
    setErr(null);
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) return setErr(await detailOf(r, fallback));
      const d: AuditSeed | AuditSeed[] = await r.json();
      setSeeds((prev) =>
        Array.isArray(d) ? [...prev, ...d] : replace ? prev.map((s) => (s.id === d.id ? d : s)) : [...prev, d],
      );
      if (Array.isArray(d)) {
        setPanel(null);
        setAll(true); // the new ones are at the end of the list
      }
    } catch {
      setErr(`${fallback}: the server is unreachable.`);
    } finally {
      setWorking(null);
    }
  }

  const generate = () =>
    seedTool("generate", `/api/audits/${audit.id}/seeds/generate`, { n: 8, guidance }, "Could not write seeds");

  async function patchSeed(seed: AuditSeed, body: Record<string, unknown>) {
    const r = await fetch(`/api/audits/${audit.id}/seeds/${seed.id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) return setErr(await detailOf(r, "Could not update the seed"));
    const updated: AuditSeed = await r.json();
    setSeeds((prev) => prev.map((s) => (s.id === seed.id ? updated : s)));
  }

  async function removeSeed(seed: AuditSeed) {
    if (!confirm(`Delete seed "${seed.title || seed.instruction.slice(0, 40)}"?`)) return;
    const r = await fetch(`/api/audits/${audit.id}/seeds/${seed.id}`, { method: "DELETE" });
    if (!r.ok) return setErr(await detailOf(r, "Could not delete the seed"));
    setSeeds((prev) => prev.filter((s) => s.id !== seed.id));
  }

  async function attach(seed: AuditSeed, file: File | undefined) {
    if (!file) return;
    const text = prompt(`What does ${file.name} show or say? Judges that cannot see or hear it read this.`) ?? "";
    await seedTool(`attach:${seed.id}`, `/api/audits/${audit.id}/seeds/${seed.id}/attachments`,
      { name: file.name, data: await fileBase64(file), text }, "Could not attach the file", true);
  }

  async function detach(seed: AuditSeed, asset: string) {
    const r = await fetch(`/api/audits/${audit.id}/seeds/${seed.id}/attachments/${asset}`, { method: "DELETE" });
    if (!r.ok) return setErr(await detailOf(r, "Could not remove the file"));
    const updated: AuditSeed = await r.json();
    setSeeds((prev) => prev.map((s) => (s.id === seed.id ? updated : s)));
  }

  async function removeAudit() {
    if (!confirm(`Delete the audit "${audit.name}", with its seeds and every run?`)) return;
    const r = await fetch(`/api/audits/${audit.id}`, { method: "DELETE" });
    if (!r.ok) return setErr(await detailOf(r, "Could not delete the audit"));
    router.push("/audits");
  }

  const visible = all ? seeds : seeds.slice(0, SHOWN);

  return (
    <div className="space-y-5">
      {/* ── result + run ── */}
      <section className="reveal card grid items-center gap-5 px-5 py-4 sm:grid-cols-[1fr_auto]">
        {last ? (
          <Link href={`/audits/${audit.id}/runs/${last.id}`} className="group flex items-center gap-4">
            <span className={clsx("font-display text-[40px] font-extrabold leading-none tracking-tight",
              last.status === "RUNNING" || rate == null ? "text-fg-faint" : rate > 0 ? "text-fail" : "text-ok")}>
              {last.status === "RUNNING" ? "…" : formatRate(rate)}
            </span>
            <span className="text-[12.5px] leading-relaxed text-fg-muted">
              <span className="block text-fg transition-colors group-hover:text-signal">
                {last.status === "RUNNING"
                  ? `Running — ${runProgress(last)}`
                  : rate == null ? "The last run measured nothing" : "of fair tests showed the behaviour"}
              </span>
              <span className="font-mono text-[11px] text-fg-faint">
                <TimeAgo ts={last.created_at} /> · {formatUsd(last.spend_usd)}
                {last.mode === "explore" ? " · explore run" : ""}
              </span>
            </span>
          </Link>
        ) : (
          <p className="text-[13px] text-fg-muted">
            Not run yet. A run plays every enabled seed against{" "}
            <span className="font-mono text-[12.5px] text-fg">{audit.config.target.model}</span> and
            reports how often the behaviour appeared.
          </p>
        )}
        <div className="flex flex-col items-end gap-1.5">
          <button onClick={run} disabled={busy || enabled === 0} className="btn-primary">
            <IconPlay className="h-4 w-4" />
            {busy ? "Starting…" : mode === "explore" ? "Explore" : "Run audit"}
          </button>
          <span className="font-mono text-[11px] text-fg-faint">
            {enabled === 0
              ? "add a seed first"
              : `${enabled * reps} rollout${enabled * reps === 1 ? "" : "s"} · up to ${formatUsd(audit.config.limits.budget_usd)}`}
            {" · "}
            <button onClick={() => setOptions((o) => !o)} aria-expanded={options} className="underline-offset-2 hover:text-signal hover:underline">
              options
            </button>
          </span>
        </div>
        {options && (
          <div className="flex flex-wrap items-start gap-x-8 gap-y-3 border-t border-line pt-4 sm:col-span-2">
            <div>
              <label className={LABEL} htmlFor={`${uid}-reps`}>Times per seed</label>
              <span className="mt-1 block w-24">
                <input id={`${uid}-reps`} type="number" min={1} max={10} value={reps}
                       onChange={(e) => setReps(Math.min(10, Math.max(1, Number(e.target.value) || 1)))}
                       className={FIELD} />
              </span>
            </div>
            <div className="min-w-0 flex-1">
              <div className={LABEL}>Kind of run</div>
              <div className="mt-1 flex gap-1.5">
                <Pill on={mode === "measure"} onClick={() => setMode("measure")}>Measure</Pill>
                <Pill on={mode === "explore"} onClick={() => setMode("explore")}>Explore</Pill>
              </div>
              <p className="mt-1.5 max-w-xl text-[12px] leading-relaxed text-fg-faint">
                {mode === "measure"
                  ? "Every rollout is independent, so the rate is the audit's result."
                  : "Finds more, measures less: rollouts learn from each other and a second pass digs into what worked. Its rate is not the audit's result."}
              </p>
            </div>
          </div>
        )}
        {err && <p role="alert" className="text-[12px] text-fail sm:col-span-2">{err}</p>}
      </section>

      {/* ── seeds ── */}
      <section className="reveal card overflow-hidden" style={{ animationDelay: "60ms" }}>
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-3">
          <span className="text-[13px] font-semibold text-fg">
            Seeds{" "}
            <span className="font-mono text-[11px] font-normal text-fg-faint">
              {seeds.length === 0 ? "" : enabled === seeds.length ? `(${seeds.length})` : `(${enabled} of ${seeds.length} on)`}
            </span>
          </span>
          {seeds.length > 0 && (
            <span className="flex gap-1.5">
              <Pill on={panel === "generate"} onClick={() => setPanel(panel === "generate" ? null : "generate")}>Write for me</Pill>
              {tags.length > 0 && (
                <Pill on={panel === "import"} onClick={() => setPanel(panel === "import" ? null : "import")}>From library</Pill>
              )}
              <Pill on={panel === "write"} onClick={() => setPanel(panel === "write" ? null : "write")}>Write my own</Pill>
            </span>
          )}
        </div>

        {panel === "generate" && (
          <div className="flex flex-wrap items-center gap-2 border-b border-line bg-ink-900/40 px-4 py-3">
            <span className="min-w-[220px] flex-1">
              <input aria-label="What kind of situations (optional)" className={clsx(FIELD, "font-sans")}
                     value={guidance} onChange={(e) => setGuidance(e.target.value)}
                     placeholder="Optional: what kind of situations? e.g. angry customers, late at night" />
            </span>
            <button className="btn-primary" disabled={working !== null || !audit.behavior} onClick={generate}>
              {working === "generate" ? "Writing…" : "Write 8 seeds"}
            </button>
          </div>
        )}
        {panel === "import" && (
          <div className="flex flex-wrap items-center gap-2 border-b border-line bg-ink-900/40 px-4 py-3">
            <span className="w-64">
              <select aria-label="Topic" value={tag} onChange={(e) => setTag(e.target.value)} className={FIELD}>
                <option value="">Choose a topic…</option>
                {tags.map((t) => (
                  <option key={t.tag} value={t.tag}>{t.tag.replaceAll("_", " ")} ({t.count})</option>
                ))}
              </select>
            </span>
            <button className="btn-primary" disabled={working !== null || !tag}
                    onClick={() => seedTool("import", `/api/audits/${audit.id}/seeds/import`, { tag }, "Could not import seeds")}>
              {working === "import" ? "Importing…" : "Import"}
            </button>
            <span className="text-[12px] text-fg-faint">Petri&apos;s research seeds: best for auditing a model.</span>
          </div>
        )}
        {panel === "write" && (
          <SeedForm auditId={audit.id} onSaved={(s) => { setSeeds((prev) => [...prev, s]); setPanel(null); setAll(true); }} />
        )}

        {seeds.length === 0 && panel === null && (
          <div className="px-4 py-12 text-center">
            <p className="mx-auto max-w-md text-[13px] leading-relaxed text-fg-muted">
              A seed is a situation for the attacker to play out. Have them written from the
              behaviour you described, or write your own.
            </p>
            <div className="mt-4 flex items-center justify-center gap-3">
              <button className="btn-primary" disabled={working !== null || !audit.behavior} onClick={generate}>
                {working === "generate" ? "Writing…" : "Write 8 seeds for me"}
              </button>
              <button className="btn-ghost" onClick={() => setPanel("write")}>Write my own</button>
              {tags.length > 0 && <button className="btn-ghost" onClick={() => setPanel("import")}>From library</button>}
            </div>
          </div>
        )}

        {visible.map((s) => {
          const isOpen = open === s.id;
          return (
            <div key={s.id} className="border-b border-line/50 last:border-0">
              <div className="grid grid-cols-[1fr_auto] items-center gap-3 px-4 py-2.5">
                <button onClick={() => { setOpen(isOpen ? null : s.id); setAxes([]); }} aria-expanded={isOpen}
                        className="flex min-w-0 items-center gap-2 text-left">
                  <IconChevron className={clsx("h-3.5 w-3.5 shrink-0 text-fg-faint transition-transform", isOpen && "rotate-90")} />
                  <span className={clsx("truncate text-[13px]", s.enabled ? "text-fg" : "text-fg-faint")}>
                    {s.title || s.instruction.slice(0, 80)}
                  </span>
                  {(s.attachments?.length ?? 0) > 0 && (
                    <span className="shrink-0 font-mono text-[10.5px] text-fg-faint" title="files the attacker can send">
                      {s.attachments.length} file{s.attachments.length === 1 ? "" : "s"}
                    </span>
                  )}
                  {s.origin === "variation" && <span className="shrink-0 font-mono text-[10.5px] uppercase tracking-wide text-fg-faint">variation</span>}
                </button>
                <Toggle checked={s.enabled} onChange={(next) => patchSeed(s, { enabled: next })} label={s.enabled ? "on" : "off"} />
              </div>
              {isOpen && (
                <div className="space-y-3 px-4 pb-4 pl-[38px]">
                  <p className="whitespace-pre-wrap text-[12.5px] leading-relaxed text-fg-muted">{s.instruction}</p>
                  {(s.attachments?.length ?? 0) > 0 && (
                    <div className="flex flex-wrap gap-2">
                      {s.attachments.map((a) => (
                        <span key={a.asset} className="inline-flex items-center gap-1.5 rounded-md border border-line px-1.5 py-1 font-mono text-[11px] text-fg-muted" title={a.text || a.name}>
                          {a.kind === "image" ? (
                            // eslint-disable-next-line @next/next/no-img-element -- a private, hash-addressed file behind our own proxy
                            <img src={`/api/audit-assets/${a.asset}`} alt={a.text || a.name} className="h-6 w-6 rounded object-cover" />
                          ) : "♪"}
                          {a.name}
                          <button onClick={() => detach(s, a.asset)} aria-label={`Remove ${a.name}`} className="text-fg-faint hover:text-fail">×</button>
                        </span>
                      ))}
                    </div>
                  )}
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                    <label className={clsx(LINK, "cursor-pointer")}>
                      {working === `attach:${s.id}` ? "Uploading…" : "Attach an image or audio"}
                      <input type="file" accept="image/png,image/jpeg,image/gif,image/webp,audio/mpeg,audio/wav" className="sr-only"
                             onChange={(e) => { void attach(s, e.target.files?.[0]); e.target.value = ""; }} />
                    </label>
                    <button onClick={() => removeSeed(s)} className={clsx(LINK, "hover:text-fail")}>Delete</button>
                  </div>
                  {variations.length > 0 && (
                    <div>
                      <div className={LABEL}>Try the same situation with</div>
                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                        {variations.map((v) => (
                          <Pill key={v.name} title={v.description} on={axes.includes(v.name)}
                                onClick={() => setAxes((cur) => (cur.includes(v.name) ? cur.filter((x) => x !== v.name) : [...cur, v.name]))}>
                            {v.name.replaceAll("_", " ")}
                          </Pill>
                        ))}
                        {axes.length > 0 && (
                          <button className="btn-ghost text-[12px]" disabled={working !== null}
                                  onClick={async () => {
                                    await seedTool(`vary:${s.id}`, `/api/audits/${audit.id}/seeds/${s.id}/variations`, { dimensions: axes }, "Could not write variations");
                                    setAxes([]);
                                  }}>
                            {working === `vary:${s.id}` ? "Writing…" : `Write ${axes.length} variation${axes.length === 1 ? "" : "s"}`}
                          </button>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
        {seeds.length > SHOWN && (
          <button onClick={() => setAll((a) => !a)} className={clsx(LINK, "block w-full border-t border-line/50 px-4 py-2.5 text-center")}>
            {all ? "Show fewer" : `Show all ${seeds.length}`}
          </button>
        )}
      </section>

      {/* ── runs ── */}
      {audit.runs.length > 0 && (
        <section className="reveal card overflow-hidden" style={{ animationDelay: "100ms" }}>
          <div className="border-b border-line px-4 py-3 text-[13px] font-semibold text-fg">
            Runs <span className="font-mono text-[11px] font-normal text-fg-faint">({audit.runs.length})</span>
          </div>
          {audit.runs.map((r) => <RunRow key={r.id} auditId={audit.id} run={r} />)}
        </section>
      )}

      {/* ── settings: set once, rarely read again ── */}
      <details ref={settings} className="reveal card group" style={{ animationDelay: "140ms" }}>
        <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-[13px] font-semibold text-fg">
          <IconChevron className="h-3.5 w-3.5 text-fg-faint transition-transform group-open:rotate-90" />
          Settings
          <span className="truncate font-mono text-[11px] font-normal text-fg-faint">
            {audit.config.target.model} · {audit.config.environment?.mode === "sandbox" ? "real machine" : "invented tool results"} ·{" "}
            {formatUsd(audit.config.limits.budget_usd)} per run
          </span>
          {!editing && (
            <button type="button" className="ml-auto shrink-0 text-[12px] font-normal text-fg-faint transition-colors hover:text-signal"
                    onClick={(e) => {
                      e.preventDefault(); // the summary would toggle the section shut
                      if (settings.current) settings.current.open = true;
                      setEditing(true);
                    }}>
              Edit
            </button>
          )}
        </summary>
        {editing ? (
          <SettingsForm audit={audit} catalog={catalog} onClose={() => setEditing(false)} />
        ) : (<>
        <dl className="grid gap-x-6 gap-y-3 border-t border-line px-4 py-4 text-[12.5px] sm:grid-cols-[150px_1fr]">
          <Setting k="Target">
            {audit.config.target.model} ·{" "}
            {audit.config.target.control === "fixed"
              ? `your agent's prompt and ${audit.config.target.tools.length} tool${audit.config.target.tools.length === 1 ? "" : "s"}`
              : "prompt and tools built by the attacker"}
          </Setting>
          <Setting k="Environment">
            {audit.config.environment?.mode === "sandbox"
              ? `a real machine (${(audit.config.environment.packs ?? ["dev"]).join(", ")})` +
                (audit.config.environment.network === "allowlist" ? ", allowlisted web access" : ", no network")
              : "the attacker invents tool results"}
            {audit.config.environment?.mode === "sandbox" && (
              <PackTools packs={audit.config.environment.packs ?? ["dev"]}
                         network={audit.config.environment.network === "allowlist"} catalog={catalog} />
            )}
          </Setting>
          {(audit.config.attacker.media?.length ?? 0) > 0 && (
            <Setting k="Attacker can send">{audit.config.attacker.media!.map((m) => (m === "voice" ? "voice messages" : "images")).join(", ")}</Setting>
          )}
          <Setting k="Judges">
            {audit.config.judge_models.length ? audit.config.judge_models.join(", ") : "server default"}
          </Setting>
          <Setting k="Budget">{formatUsd(audit.config.limits.budget_usd)} per run · {audit.config.attacker.max_turns} attacker turns per rollout</Setting>
          <Setting k="What is judged">
            <ul className="space-y-1.5">
              {audit.dimensions.map((d) => (
                <li key={d.name} className="flex items-start gap-2">
                  <Badge variant={d.kind === "behaviour" ? "signal" : d.kind === "validity" ? "warn" : "neutral"}
                         title={d.kind === "validity"
                           ? "A rollout that scores high here is excluded from the result, not counted as a failure."
                           : undefined}>
                    {d.kind === "validity" ? "fair test?" : d.kind}
                  </Badge>
                  <span className="min-w-0 text-fg-muted">
                    <span className="font-mono text-fg">{d.name.replaceAll("_", " ")}</span>{" "}
                    — {d.name === "behaviour" ? audit.behavior || d.description : d.description}
                  </span>
                </li>
              ))}
            </ul>
          </Setting>
        </dl>
        <div className="flex justify-between border-t border-line px-4 py-3">
          <button onClick={() => setEditing(true)} className="btn-ghost">Edit settings</button>
          <button onClick={removeAudit} className="btn-ghost text-fail hover:border-fail/40 hover:text-fail">
            <IconTrash className="h-3.5 w-3.5" /> Delete audit
          </button>
        </div>
        </>)}
      </details>
    </div>
  );
}

function Pill({ on, onClick, children, title }: { on: boolean; onClick: () => void; children: React.ReactNode; title?: string }) {
  return (
    <button type="button" onClick={onClick} aria-pressed={on} title={title}
            className={clsx("rounded-md border px-2.5 py-1 text-[12px] transition-colors",
              on ? "border-signal/50 bg-signal/10 text-fg" : "border-line text-fg-muted hover:border-line-bright hover:text-fg")}>
      {children}
    </button>
  );
}

/** Edit what an audit runs with. Saving replaces the audit's config for FUTURE runs only: every
 *  past run carries its own snapshot. The target's prompt and tools, and the judged dimensions,
 *  are kept as they are. */
function SettingsForm({ audit, catalog, onClose }: { audit: Audit; catalog: AuditCatalog; onClose: () => void }) {
  const router = useRouter();
  const cfg = audit.config;
  const canSandbox = Boolean(catalog.sandbox?.available);
  const [models, setModels] = useState<JudgeModelOption[]>([]);
  const [name, setName] = useState(audit.name);
  const [behavior, setBehavior] = useState(audit.behavior);
  const [model, setModel] = useState(cfg.target.model);
  const [control, setControl] = useState(cfg.target.control);
  const [systemPrompt, setSystemPrompt] = useState(cfg.target.system_prompt);
  const [toolsText, setToolsText] = useState(cfg.target.tools.length ? JSON.stringify(cfg.target.tools, null, 2) : "");
  const [dims, setDims] = useState<string[]>(cfg.dimensions.map((d) => d.name));
  const [attacker, setAttacker] = useState(cfg.attacker.models[0] ?? "");
  const [bestOf, setBestOf] = useState(String(cfg.attacker.best_of ?? 1));
  const [realism, setRealism] = useState(Boolean(cfg.attacker.realism_filter));
  const [sandbox, setSandbox] = useState(cfg.environment?.mode === "sandbox");
  const [packs, setPacks] = useState<string[]>(cfg.environment?.packs?.length ? cfg.environment.packs : ["dev"]);
  const [network, setNetwork] = useState(cfg.environment?.network === "allowlist");
  const [media, setMedia] = useState<string[]>(cfg.attacker.media ?? []);
  const [judges, setJudges] = useState<string[]>(cfg.judge_models);
  const [budget, setBudget] = useState(String(cfg.limits.budget_usd));
  const [turns, setTurns] = useState(String(cfg.attacker.max_turns));
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void listJudgeModels().then((m) => setModels(m.models.filter((x) => x.kind !== "decision"))).catch(() => {});
  }, []);

  const flip = (list: string[], set: (v: string[]) => void, key: string) =>
    set(list.includes(key) ? list.filter((k) => k !== key) : [...list, key]);
  // A model saved on the audit but missing from the curated list still has to be selectable.
  const ids = [...new Set([...models.map((m) => m.id), cfg.target.model, ...cfg.judge_models])].filter(Boolean);
  const label = (id: string) => models.find((m) => m.id === id)?.label ?? id;
  // The catalog's behaviours, plus any dimension this audit has that the catalog does not (a custom one).
  const known = [...catalog.behaviours, ...cfg.dimensions.filter((d) => !catalog.behaviours.some((c) => c.name === d.name))];

  async function save() {
    setErr(null);
    let tools = cfg.target.tools;
    if (control === "fixed") {
      try {
        tools = parseTools(toolsText);
      } catch (e) {
        return setErr(e instanceof Error ? e.message : "The tools JSON is not valid.");
      }
    }
    setBusy(true);
    const useBox = sandbox && canSandbox;
    const config = {
      ...cfg,
      target: { ...cfg.target, model, control, system_prompt: control === "fixed" ? systemPrompt : "", tools: control === "fixed" ? tools : [] },
      attacker: {
        ...cfg.attacker, models: attacker ? [attacker] : [], max_turns: Number(turns) || cfg.attacker.max_turns,
        best_of: Number(bestOf) || 1, realism_filter: realism, media,
      },
      // A dimension the audit already had keeps its own rubric and threshold; a new one is sent
      // by name and the server fills it in from the catalog.
      dimensions: dims.map((n) => cfg.dimensions.find((d) => d.name === n) ?? n),
      environment: useBox
        ? { mode: "sandbox", packs: packs.length ? packs : ["dev"], network: network && catalog.sandbox?.network ? "allowlist" : "none" }
        : { mode: "synthetic", packs: [], network: "none" },
      judge_models: judges,
      limits: { ...cfg.limits, budget_usd: Number(budget) || cfg.limits.budget_usd },
    };
    try {
      const r = await fetch(`/api/audits/${audit.id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ name, behavior, config }),
      });
      if (!r.ok) return setErr(await detailOf(r, "Could not save the settings"));
      router.refresh();
      onClose();
    } catch {
      setErr("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="border-t border-line">
      <dl className="grid gap-x-6 gap-y-4 px-4 py-4 text-[12.5px] sm:grid-cols-[150px_1fr]">
        <Setting k="Name">
          <input aria-label="Name" className={FIELD} value={name} onChange={(e) => setName(e.target.value)} />
        </Setting>
        <Setting k="Looks for">
          <textarea aria-label="Behaviour" rows={3} className={FIELD} value={behavior} onChange={(e) => setBehavior(e.target.value)} />
        </Setting>
        <Setting k="Target model">
          <select aria-label="Target model" className={FIELD} value={model} onChange={(e) => setModel(e.target.value)}>
            {ids.map((id) => <option key={id} value={id}>{label(id)}</option>)}
          </select>
        </Setting>
        <Setting k="Prompt and tools">
          <div className="flex flex-wrap gap-1.5">
            <Pill on={control === "auditor"} onClick={() => setControl("auditor")}
                  title="The attacker writes a system prompt and tools to fit each situation">Built by the attacker</Pill>
            <Pill on={control === "fixed"} onClick={() => setControl("fixed")}
                  title="This exact system prompt and these tools are what is tested">Fixed</Pill>
          </div>
          {control === "fixed" && (
            <div className="mt-2 grid gap-3 sm:grid-cols-2">
              <textarea aria-label="System prompt" rows={7} className={FIELD} value={systemPrompt}
                        onChange={(e) => setSystemPrompt(e.target.value)} placeholder="System prompt" />
              <textarea aria-label="Tools (JSON array)" rows={7} className={FIELD} value={toolsText}
                        onChange={(e) => setToolsText(e.target.value)} placeholder="Tools, as a JSON array" />
            </div>
          )}
        </Setting>
        <Setting k="Tool results">
          <div className="flex flex-wrap gap-1.5">
            <Pill on={!(sandbox && canSandbox)} onClick={() => setSandbox(false)}>Invented by the attacker</Pill>
            <Pill on={sandbox && canSandbox} onClick={() => canSandbox && setSandbox(true)}
                  title={canSandbox ? undefined : "This deployment has no sandbox configured (SANDBOX_BACKEND)"}>
              A real machine{canSandbox ? "" : " (not set up)"}
            </Pill>
          </div>
          {sandbox && canSandbox && (
            <>
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <span className="text-[12px] text-fg-faint">with</span>
                {PACKS.map((p) => (
                  <Pill key={p.key} on={packs.includes(p.key)} onClick={() => flip(packs, setPacks, p.key)} title={p.hint}>{p.label}</Pill>
                ))}
                {catalog.sandbox?.network && (
                  <Pill on={network} onClick={() => setNetwork(!network)}>Web access (allowlist)</Pill>
                )}
              </div>
              <PackTools packs={packs.length ? packs : ["dev"]} network={network} catalog={catalog} />
            </>
          )}
        </Setting>
        <Setting k="Attacker can send">
          <div className="flex flex-wrap gap-1.5">
            <Pill on={media.includes("image")} onClick={() => flip(media, setMedia, "image")}>Images</Pill>
            <Pill on={media.includes("voice")} onClick={() => flip(media, setMedia, "voice")}>Voice messages</Pill>
          </div>
        </Setting>
        <Setting k="Judges">
          <div className="flex flex-wrap gap-1.5">
            {ids.map((id) => (
              <Pill key={id} on={judges.includes(id)} onClick={() => flip(judges, setJudges, id)}>{label(id)}</Pill>
            ))}
          </div>
        </Setting>
        <Setting k="What is judged">
          <div className="flex flex-wrap gap-1.5">
            {known.filter((d) => !d.source).map((d) => (
              <Pill key={d.name} on={dims.includes(d.name)} onClick={() => flip(dims, setDims, d.name)} title={d.description}>
                {d.name.replaceAll("_", " ")}
              </Pill>
            ))}
          </div>
          {known.some((d) => d.source) && (
            <details className="mt-2" open={known.some((d) => d.source && dims.includes(d.name))}>
              <summary className="cursor-pointer text-[12px] text-fg-faint transition-colors hover:text-signal">
                {known.filter((d) => d.source).length} more from Petri
              </summary>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {known.filter((d) => d.source).map((d) => (
                  <Pill key={d.name} on={dims.includes(d.name)} onClick={() => flip(dims, setDims, d.name)} title={d.description}>
                    {d.name.replaceAll("_", " ")}
                  </Pill>
                ))}
              </div>
            </details>
          )}
          <p className="mt-1.5 text-[12px] text-fg-faint">
            {dims.length ? "The three fair-test checks are always added." : "With none chosen, the audit judges the behaviour described above. The three fair-test checks are always added."}
          </p>
        </Setting>
        <Setting k="Attacker">
          <div className="flex flex-wrap items-center gap-2 text-fg-faint">
            <span className="block w-56">
              <select aria-label="Attacker model" className={FIELD} value={attacker} onChange={(e) => setAttacker(e.target.value)}>
                <option value="">Server default</option>
                {[...new Set([...ids, attacker])].filter(Boolean).map((id) => <option key={id} value={id}>{label(id)}</option>)}
              </select>
            </span>
            <span className="block w-16"><input aria-label="Drafts per turn" title="Explore runs only" type="number" min={1} max={4} className={FIELD} value={bestOf} onChange={(e) => setBestOf(e.target.value)} /></span>
            drafts per turn (explore runs)
            <span className="ml-2"><Toggle checked={realism} onChange={setRealism} label="Realism check" /></span>
          </div>
        </Setting>
        <Setting k="Budget">
          <div className="flex flex-wrap items-center gap-2 text-fg-faint">
            $ <span className="block w-24"><input aria-label="Budget per run in USD" type="number" min={0.1} max={1000} step={0.5} className={FIELD} value={budget} onChange={(e) => setBudget(e.target.value)} /></span>
            per run ·
            <span className="block w-20"><input aria-label="Attacker turns per rollout" type="number" min={1} step={1} className={FIELD} value={turns} onChange={(e) => setTurns(e.target.value)} /></span>
            attacker turns per rollout
          </div>
        </Setting>
      </dl>
      {err && <p role="alert" className="px-4 pb-3 text-[12.5px] text-fail">{err}</p>}
      <div className="flex items-center justify-end gap-2 border-t border-line px-4 py-3">
        <span className="mr-auto text-[12px] text-fg-faint">Applies to the next run. Past runs keep the settings they ran with.</span>
        <button onClick={onClose} className="btn-ghost" disabled={busy}>Cancel</button>
        <button onClick={save} className="btn-primary" disabled={busy || !name.trim()}>{busy ? "Saving…" : "Save"}</button>
      </div>
    </div>
  );
}

function Setting({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <>
      <dt className={clsx(LABEL, "pt-0.5")}>{k}</dt>
      <dd className="min-w-0 text-fg-muted">{children}</dd>
    </>
  );
}

function RunRow({ auditId, run }: { auditId: string; run: AuditRun }) {
  const rate = run.summary?.elicitation_rate;
  const running = run.status === "RUNNING";
  return (
    <Link href={`/audits/${auditId}/runs/${run.id}`}
          className="group grid grid-cols-[96px_1fr_auto] items-center gap-3 border-b border-line/50 px-4 py-3 text-[12.5px] transition-colors last:border-0 hover:bg-hilite/[0.025]">
      <Badge variant={running ? "info" : run.status === "CANCELLED" ? "neutral" : run.summary?.status === "NO_COVERAGE" ? "warn" : "ok"} dot={running}>
        {running ? "running" : run.status === "CANCELLED" ? "cancelled" : run.summary?.status === "NO_COVERAGE" ? "no coverage" : "done"}
      </Badge>
      <span className="min-w-0 font-mono text-[11.5px] text-fg-muted">
        {/* TimeAgo, not toLocaleString(): this component renders on the server too, in the
            server's timezone, and the two strings then disagree at hydration. */}
        <TimeAgo ts={run.created_at} /> · {runProgress(run)} · {formatUsd(run.spend_usd)}
        {run.mode === "explore" ? " · explore" : ""}
      </span>
      <span className="flex items-center gap-3">
        {!running && (
          <span className={clsx("font-display text-[16px] font-bold", rate == null ? "text-fg-faint" : rate > 0 ? "text-fail" : "text-ok")}>
            {formatRate(rate)}
          </span>
        )}
        <IconChevron className="h-4 w-4 text-fg-faint transition-colors group-hover:text-signal" />
      </span>
    </Link>
  );
}

function SeedForm({ auditId, onSaved }: { auditId: string; onSaved: (s: AuditSeed) => void }) {
  const uid = useId();
  const [title, setTitle] = useState("");
  const [instruction, setInstruction] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      const r = await fetch(`/api/audits/${auditId}/seeds`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ title, instruction }),
      });
      if (r.ok) return onSaved(await r.json());
      setErr(await detailOf(r, "Could not save the seed"));
    } catch {
      setErr("Could not save the seed: the server is unreachable.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3 border-b border-line bg-ink-900/40 px-4 py-4">
      <input aria-label="Title" className={FIELD} value={title} onChange={(e) => setTitle(e.target.value)}
             placeholder="Title — e.g. Customer insists on an out-of-policy refund" />
      <textarea id={`${uid}-instruction`} aria-label="The situation" rows={4} className={clsx(FIELD, "font-sans")} value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
                placeholder="The situation: who the attacker plays, what the target has (tools, data), what happens, and what to try if the first approach fails." />
      <div className="flex items-center justify-end gap-3">
        {err && <p role="alert" className="mr-auto text-[12px] text-fail">{err}</p>}
        <button onClick={save} disabled={busy || !instruction.trim()} className="btn-primary">
          {busy ? "Saving…" : "Add seed"}
        </button>
      </div>
    </div>
  );
}
