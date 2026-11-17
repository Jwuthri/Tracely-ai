"use client";

import clsx from "clsx";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useId, useState } from "react";
import {
  formatRate,
  formatUsd,
  runProgress,
  type AuditDetail as Audit,
  type AuditRun,
  type AuditSeed,
} from "@/app/lib/audits";
import { TimeAgo } from "./TimeAgo";
import { Toggle } from "./Toggle";
import { Badge } from "./ui";
import { IconChevron, IconPlay, IconTrash } from "./icons";

const FIELD =
  "w-full rounded-lg border border-line bg-ink-700 px-2.5 py-2 font-mono text-[12.5px] text-fg placeholder:text-fg-faint transition-colors hover:border-line-bright focus:border-signal/50 focus:outline-none";
const LABEL = "font-mono text-[10px] uppercase tracking-wider text-fg-faint";

async function detailOf(r: Response, fallback: string): Promise<string> {
  const d = await r.json().catch(() => null);
  return d?.detail ?? `${fallback} (HTTP ${r.status}).`;
}

export function AuditDetail({ audit }: { audit: Audit }) {
  const router = useRouter();
  const uid = useId();
  const [seeds, setSeeds] = useState(audit.seeds);
  const [reps, setReps] = useState(audit.config.limits.reps);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(audit.seeds.length === 0);
  const enabled = seeds.filter((s) => s.enabled).length;
  const target = audit.config.target;
  const judges = audit.config.judge_models;

  async function run() {
    setBusy(true);
    setErr(null);
    try {
      const r = await fetch(`/api/audits/${audit.id}/runs`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ reps }),
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

  async function removeAudit() {
    if (!confirm(`Delete the audit "${audit.name}", with its seeds and every run?`)) return;
    const r = await fetch(`/api/audits/${audit.id}`, { method: "DELETE" });
    if (!r.ok) return setErr(await detailOf(r, "Could not delete the audit"));
    router.push("/audits");
  }

  return (
    <div className="space-y-5">
      <div className="reveal flex flex-wrap items-end justify-between gap-4">
        <div className="card grid flex-1 grid-cols-2 divide-x divide-line/60 sm:grid-cols-4">
          <Meta k="Target" v={target.model} />
          <Meta k="Prompt and tools" v={target.control === "fixed" ? `fixed · ${target.tools.length} tools` : "built by the attacker"} />
          <Meta k="Judges" v={judges.length ? `${judges.length} model${judges.length === 1 ? "" : "s"}` : "server default"} />
          <Meta k="Budget per run" v={formatUsd(audit.config.limits.budget_usd)} />
        </div>
        <div className="flex flex-col items-end gap-1.5">
          <div className="flex items-end gap-2">
            <div>
              <label className={LABEL} htmlFor={`${uid}-reps`}>Repetitions</label>
              <input id={`${uid}-reps`} type="number" min={1} max={10} value={reps}
                     onChange={(e) => setReps(Math.min(10, Math.max(1, Number(e.target.value) || 1)))}
                     className={clsx(FIELD, "mt-1 w-24")} />
            </div>
            <button onClick={run} disabled={busy || enabled === 0} className="btn-primary">
              <IconPlay className="h-4 w-4" />
              {busy ? "Starting…" : `Run ${enabled * reps} rollout${enabled * reps === 1 ? "" : "s"}`}
            </button>
          </div>
          {enabled === 0 && <p className="text-[12px] text-fg-faint">Add a seed to run this audit.</p>}
          {err && <p role="alert" className="max-w-md text-right text-[12px] text-fail">{err}</p>}
        </div>
      </div>

      <section className="reveal card overflow-hidden" style={{ animationDelay: "60ms" }}>
        <div className="flex items-center justify-between border-b border-line px-4 py-3">
          <span className="text-[13px] font-semibold text-fg">
            Seeds <span className="font-mono text-[11px] text-fg-faint">({seeds.length}, {enabled} enabled)</span>
          </span>
          <button onClick={() => setAdding((a) => !a)} className="btn-ghost text-[12.5px]">
            {adding ? "Cancel" : "+ New seed"}
          </button>
        </div>
        {adding && (
          <SeedForm
            auditId={audit.id}
            onSaved={(s) => {
              setSeeds((prev) => [...prev, s]);
              setAdding(false);
            }}
          />
        )}
        {seeds.length === 0 && !adding ? (
          <div className="px-4 py-12 text-center text-[13px] text-fg-faint">
            No seeds yet. A seed is one instruction to the attacker: the situation to set up and
            what to try.
          </div>
        ) : (
          seeds.map((s) => (
            <div key={s.id} className="group grid grid-cols-[1fr_auto] items-start gap-3 border-b border-line/50 px-4 py-3 last:border-0">
              <div className="min-w-0">
                <div className={clsx("text-[13px]", s.enabled ? "text-fg" : "text-fg-faint")}>
                  {s.title || <span className="text-fg-faint">(untitled)</span>}
                  {s.origin !== "manual" && (
                    <span className="ml-2 font-mono text-[10.5px] uppercase tracking-wide text-fg-faint">{s.origin}</span>
                  )}
                </div>
                <p className="mt-1 whitespace-pre-wrap text-[12.5px] leading-relaxed text-fg-muted">{s.instruction}</p>
              </div>
              <div className="flex items-center gap-2">
                <Toggle checked={s.enabled} onChange={(next) => patchSeed(s, { enabled: next })}
                        label={s.enabled ? "enabled" : "off"} />
                <button onClick={() => removeSeed(s)} aria-label={`Delete ${s.title || "seed"}`} title="Delete seed"
                        className="grid h-7 w-7 place-items-center rounded-md border border-transparent text-fg-faint opacity-0 transition-all hover:border-fail/30 hover:bg-fail/10 hover:text-fail focus-visible:opacity-100 focus-visible:outline-none group-hover:opacity-100">
                  <IconTrash className="h-[15px] w-[15px]" />
                </button>
              </div>
            </div>
          ))
        )}
      </section>

      <section className="reveal card overflow-hidden" style={{ animationDelay: "100ms" }}>
        <div className="border-b border-line px-4 py-3 text-[13px] font-semibold text-fg">
          Runs <span className="font-mono text-[11px] text-fg-faint">({audit.runs.length})</span>
        </div>
        {audit.runs.length === 0 ? (
          <div className="px-4 py-10 text-center text-[13px] text-fg-faint">Not run yet.</div>
        ) : (
          audit.runs.map((r) => <RunRow key={r.id} auditId={audit.id} run={r} />)
        )}
      </section>

      <section className="reveal card overflow-hidden" style={{ animationDelay: "140ms" }}>
        <div className="border-b border-line px-4 py-3 text-[13px] font-semibold text-fg">What is judged</div>
        {audit.dimensions.map((d) => (
          <div key={d.name} className="grid grid-cols-[110px_1fr] gap-3 border-b border-line/50 px-4 py-3 last:border-0">
            <Badge variant={d.kind === "behaviour" ? "signal" : d.kind === "validity" ? "warn" : "neutral"}
                   title={d.kind === "validity"
                     ? "A rollout that scores high here is excluded from the result, not counted as a failure."
                     : undefined}>
              {d.kind}
            </Badge>
            <div className="min-w-0">
              <div className="font-mono text-[12.5px] text-fg">
                {d.name} <span className="text-fg-faint">· flagged at {d.threshold}/10</span>
              </div>
              <p className="mt-1 text-[12.5px] leading-relaxed text-fg-muted">
                {d.name === "behaviour" ? audit.behavior || d.description : d.description}
              </p>
            </div>
          </div>
        ))}
      </section>

      <div className="flex justify-end">
        <button onClick={removeAudit} className="btn-ghost text-fail hover:border-fail/40 hover:text-fail">
          <IconTrash className="h-3.5 w-3.5" /> Delete audit
        </button>
      </div>
    </div>
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
      <div>
        <label className={LABEL} htmlFor={`${uid}-title`}>Title</label>
        <input id={`${uid}-title`} className={clsx(FIELD, "mt-1")} value={title} onChange={(e) => setTitle(e.target.value)}
               placeholder="Customer insists on an out-of-policy refund" />
      </div>
      <div>
        <label className={LABEL} htmlFor={`${uid}-instruction`}>Instruction to the attacker</label>
        <textarea id={`${uid}-instruction`} rows={5} className={clsx(FIELD, "mt-1 font-sans")} value={instruction}
                  onChange={(e) => setInstruction(e.target.value)}
                  placeholder="Describe the situation to set up, who the attacker plays, and what to try. Be concrete: name the tools the target should have and what a realistic day looks like." />
      </div>
      <div className="flex items-center justify-end gap-3">
        {err && <p role="alert" className="mr-auto text-[12px] text-fail">{err}</p>}
        <button onClick={save} disabled={busy || !instruction.trim()} className="btn-primary">
          {busy ? "Saving…" : "Add seed"}
        </button>
      </div>
    </div>
  );
}

function Meta({ k, v }: { k: string; v: string }) {
  return (
    <div className="min-w-0 px-4 py-3">
      <div className="font-mono text-[10px] uppercase tracking-wider text-fg-faint">{k}</div>
      <div className="mt-1 truncate font-mono text-[13px] text-fg" title={v}>{v}</div>
    </div>
  );
}
