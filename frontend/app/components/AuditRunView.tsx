"use client";

import clsx from "clsx";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  byBranch,
  finalPath,
  formatUsd,
  resultTone,
  verdictMeaning,
  verdictTone,
  type AttackerMessage,
  type AuditDimension,
  type AuditRollout,
  type AuditRunDetail,
  type DimensionResult,
  type RolloutDetail,
  type TranscriptNode,
} from "@/app/lib/audits";
import { Badge } from "./ui";
import { IconChevron } from "./icons";

const TONE_TEXT = { ok: "text-ok", fail: "text-fail", warn: "text-warn", info: "text-info", neutral: "text-fg-muted" };

export function AuditRunView({ run }: { run: AuditRunDetail }) {
  const router = useRouter();
  const [open, setOpen] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const running = run.status === "RUNNING";
  // Behaviour first, then validity, then info — the order a reader decides in.
  const dims = [...run.dimensions].sort((a, b) => rank(a) - rank(b));

  async function act(action: "cancel" | "resume") {
    setErr(null);
    const r = await fetch(`/api/audit-runs/${run.id}/${action}`, { method: "POST" });
    if (!r.ok) {
      const d = await r.json().catch(() => null);
      return setErr(d?.detail ?? `Could not ${action} the run (HTTP ${r.status}).`);
    }
    router.refresh();
  }

  return (
    <section className="card overflow-hidden">
      <div className="flex items-center justify-between border-b border-line px-4 py-3">
        <span className="text-[13px] font-semibold text-fg">
          Rollouts <span className="font-mono text-[11px] text-fg-faint">({run.rollouts.length})</span>
        </span>
        {running && (
          <span className="flex items-center gap-2">
            {err && <span role="alert" className="text-[12px] text-fail">{err}</span>}
            <button onClick={() => act("resume")} className="btn-ghost text-[12px]"
                    title="Restart the worker loop for this run if it has stopped moving">
              Nudge
            </button>
            <button onClick={() => act("cancel")} className="btn-ghost text-[12px] hover:border-fail/40 hover:text-fail">
              Cancel run
            </button>
          </span>
        )}
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] border-collapse text-[12.5px]">
          <thead>
            <tr className="border-b border-line text-left font-mono text-[10px] uppercase tracking-wider text-fg-faint">
              <th className="px-4 py-2 font-medium">Verdict</th>
              <th className="px-2 py-2 font-medium">Seed</th>
              {dims.map((d) => (
                <th key={d.name} className="px-2 py-2 text-center font-medium" title={d.description}>
                  {d.name.replaceAll("_", " ")}
                </th>
              ))}
              <th className="px-2 py-2 text-right font-medium">Turns</th>
              <th className="px-4 py-2 text-right font-medium">Cost</th>
            </tr>
          </thead>
          <tbody>
            {run.rollouts.map((r) => (
              <RolloutRows key={r.id} rollout={r} dims={dims} open={open === r.id}
                           onToggle={() => setOpen(open === r.id ? null : r.id)} />
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

const rank = (d: AuditDimension) => (d.kind === "behaviour" ? 0 : d.kind === "validity" ? 1 : 2);

function RolloutRows({
  rollout,
  dims,
  open,
  onToggle,
}: {
  rollout: AuditRollout;
  dims: AuditDimension[];
  open: boolean;
  onToggle: () => void;
}) {
  const done = rollout.phase === "DONE";
  const byName = new Map(rollout.results.map((r) => [r.name, r]));
  const why = rollout.detail.error || rollout.detail.reason;
  return (
    <>
      <tr
        onClick={onToggle}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle();
          }
        }}
        tabIndex={0}
        aria-expanded={open}
        className="cursor-pointer border-b border-line/50 transition-colors hover:bg-hilite/[0.025] focus-visible:bg-hilite/[0.025] focus-visible:outline-none"
      >
        <td className="px-4 py-2.5">
          {done ? (
            <Badge variant={verdictTone(rollout.verdict)} title={verdictMeaning(rollout.verdict)}>
              {rollout.verdict.toLowerCase()}
            </Badge>
          ) : (
            <Badge variant={rollout.phase === "PENDING" ? "neutral" : "info"} dot={rollout.phase !== "PENDING"}>
              {rollout.phase.toLowerCase()}
            </Badge>
          )}
        </td>
        <td className="max-w-[320px] px-2 py-2.5">
          <div className="flex items-center gap-1.5">
            <IconChevron className={clsx("h-3.5 w-3.5 shrink-0 text-fg-faint transition-transform", open && "rotate-90")} />
            <span className="truncate text-fg">{rollout.title || "(untitled seed)"}</span>
            {rollout.rep > 0 && <span className="font-mono text-[10.5px] text-fg-faint">#{rollout.rep + 1}</span>}
          </div>
          {why && <div className="mt-0.5 truncate pl-5 font-mono text-[11px] text-fg-faint" title={why}>{why}</div>}
        </td>
        {dims.map((d) => (
          <td key={d.name} className="px-2 py-2.5 text-center">
            <ScoreCell result={byName.get(d.name)} />
          </td>
        ))}
        <td className="px-2 py-2.5 text-right font-mono text-[11.5px] text-fg-muted">
          {rollout.detail.turns ?? "—"}
          {rollout.detail.branches ? <span className="text-fg-faint" title="times the attacker rewound the target"> ↺{rollout.detail.branches}</span> : null}
        </td>
        <td className="px-4 py-2.5 text-right font-mono text-[11.5px] text-fg-muted">{formatUsd(rollout.spend_usd)}</td>
      </tr>
      {open && (
        <tr className="border-b border-line/50 bg-ink-900/40">
          <td colSpan={dims.length + 4} className="px-4 py-4">
            <RolloutPanel rolloutId={rollout.id} live={!done} />
          </td>
        </tr>
      )}
    </>
  );
}

function ScoreCell({ result }: { result?: DimensionResult }) {
  if (!result) return <span className="text-fg-faint">·</span>;
  if (!result.graded) {
    return <span className="font-mono text-[11px] text-warn" title="Too few judges answered for a panel result">n/a</span>;
  }
  const panel = result.judges.filter((j) => j.mean !== null);
  return (
    <span
      className={clsx("font-mono text-[12.5px] font-semibold", TONE_TEXT[resultTone(result)])}
      title={panel.map((j) => `${j.model}: ${j.mean}`).join("\n")}
    >
      {result.value}
      {result.disputed && (
        <span className="ml-0.5 text-warn" title="The judges disagreed about which side of the threshold this falls on">≠</span>
      )}
    </span>
  );
}

type Tab = "conversation" | "attacker" | "judges";

function RolloutPanel({ rolloutId, live }: { rolloutId: string; live: boolean }) {
  const [data, setData] = useState<RolloutDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("conversation");

  useEffect(() => {
    let stop = false;
    async function load() {
      try {
        const r = await fetch(`/api/audit-rollouts/${rolloutId}`, { cache: "no-store" });
        const d = await r.json().catch(() => null);
        if (stop) return;
        if (r.ok) setData(d);
        else setErr(d?.detail ?? `Could not load the transcript (HTTP ${r.status}).`);
      } catch {
        if (!stop) setErr("Could not load the transcript: the server is unreachable.");
      }
    }
    void load();
    // A rollout still in flight grows; a finished one never changes.
    const t = live ? setInterval(load, 4000) : null;
    return () => {
      stop = true;
      if (t) clearInterval(t);
    };
  }, [rolloutId, live]);

  if (err) return <p role="alert" className="text-[12.5px] text-fail">{err}</p>;
  if (!data) return <p className="text-[12.5px] text-fg-faint">Loading the transcript…</p>;
  const t = data.transcript;

  return (
    <div className="space-y-3">
      {data.seed.instruction && (
        <div className="rounded-lg border border-line bg-ink-800/60 px-3 py-2.5">
          <div className="font-mono text-[10px] uppercase tracking-wider text-fg-faint">Seed</div>
          <p className="mt-1 whitespace-pre-wrap text-[12.5px] leading-relaxed text-fg-muted">{data.seed.instruction}</p>
        </div>
      )}
      <div role="tablist" className="flex flex-wrap items-center gap-1.5">
        {([
          ["conversation", `Conversation${t.branches.length > 1 ? ` · ${t.branches.length} branches` : ""}`],
          ["attacker", "Attacker"],
          ["judges", "Judges"],
        ] as const).map(([key, label]) => (
          <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)}
                  className={clsx("rounded-md border px-2.5 py-1 font-mono text-[11.5px] transition-colors",
                    tab === key ? "border-signal/50 bg-signal/10 text-signal" : "border-line text-fg-muted hover:border-line-bright")}>
            {label}
          </button>
        ))}
        {(data.detail.trace_ids?.length ?? 0) > 0 && (
          <a href={`/sessions/${encodeURIComponent(data.conversation_id)}`}
             className="ml-auto font-mono text-[11.5px] text-fg-faint underline decoration-line-bright underline-offset-2 transition-colors hover:text-signal">
            final conversation in Traces ↗
          </a>
        )}
      </div>
      {tab === "conversation" && <Conversation data={data} />}
      {tab === "attacker" && <Attacker messages={t.attacker} model={data.detail.attacker_model} />}
      {tab === "judges" && <Judges results={data.results} />}
    </div>
  );
}

function Conversation({ data }: { data: RolloutDetail }) {
  const t = data.transcript;
  if (t.nodes.length === 0) {
    return <p className="text-[12.5px] text-fg-faint">The target has not been sent anything yet.</p>;
  }
  const survived = finalPath(t.nodes, t.head);
  const branches = byBranch(t.nodes, t.branches);
  return (
    <div className="space-y-3">
      {t.control === "fixed" && t.system_prompt && (
        <Bubble label="System prompt · fixed, under test" tone="neutral">{t.system_prompt}</Bubble>
      )}
      {t.tools.length > 0 && (
        <div className="font-mono text-[11.5px] text-fg-faint">
          tools: {t.tools.map((x) => x.name).join(", ")}
        </div>
      )}
      {branches.map((b) => (
        <div key={b.n} className={clsx("space-y-2", b.n > 0 && "border-l-2 border-warn/40 pl-3")}>
          {b.n > 0 && (
            <div className="font-mono text-[11px] text-warn">
              ↺ branch {b.n} — the attacker {b.from ? `rewound the target to ${b.from}` : "restarted the conversation"}
            </div>
          )}
          {b.nodes.map((n) => <Node key={n.id} node={n} abandoned={!survived.has(n.id)} />)}
        </div>
      ))}
    </div>
  );
}

function Node({ node, abandoned }: { node: TranscriptNode; abandoned: boolean }) {
  const label =
    node.role === "system" ? "System prompt · written by the attacker"
    : node.role === "user" ? "User · the attacker"
    : node.role === "tool" ? `Tool result · ${node.name ?? ""} · ${node.resolution === "synthetic" || !node.resolution ? "invented by the attacker" : "real"}`
    : "Target";
  return (
    <div className={clsx(abandoned && "opacity-55")} title={abandoned ? "Rolled back: the target no longer remembers this" : undefined}>
      <Bubble label={`${node.id} · ${label}`} tone={node.role === "assistant" ? "signal" : node.role === "tool" ? "info" : "neutral"}>
        {node.reasoning && (
          <span className="mb-2 block border-l-2 border-line pl-2 italic text-fg-faint">{node.reasoning}</span>
        )}
        {node.content || (node.tool_calls?.length ? "" : <span className="text-fg-faint">(empty)</span>)}
        {(node.tool_calls ?? []).map((c) => (
          <span key={c.id} className="mt-1.5 block font-mono text-[11.5px] text-info">
            → {c.name}({JSON.stringify(c.args)})
          </span>
        ))}
      </Bubble>
    </div>
  );
}

function Bubble({ label, tone, children }: { label: string; tone: "signal" | "info" | "neutral"; children: React.ReactNode }) {
  return (
    <div className={clsx("rounded-lg border px-3 py-2.5",
      tone === "signal" ? "border-signal/25 bg-signal/[0.04]" : tone === "info" ? "border-info/20 bg-info/[0.04]" : "border-line bg-ink-800/60")}>
      <div className="font-mono text-[10px] uppercase tracking-wider text-fg-faint">{label}</div>
      <div className="mt-1 whitespace-pre-wrap break-words text-[12.5px] leading-relaxed text-fg">{children}</div>
    </div>
  );
}

function Attacker({ messages, model }: { messages: AttackerMessage[]; model?: string }) {
  return (
    <div className="space-y-2">
      {model && <div className="font-mono text-[11.5px] text-fg-faint">attacker model: {model}</div>}
      {messages.map((m, i) =>
        m.role === "assistant" ? (
          <Bubble key={i} label="Attacker" tone="signal">
            {m.content || null}
            {(m.tool_calls ?? []).map((c) => (
              <span key={c.id} className="mt-1.5 block font-mono text-[11.5px] text-info">
                → {c.function.name}({c.function.arguments})
              </span>
            ))}
          </Bubble>
        ) : (
          <Bubble key={i} label={m.role === "tool" ? "Result" : "Instruction"} tone="neutral">
            {m.content}
          </Bubble>
        ),
      )}
    </div>
  );
}

function Judges({ results }: { results: DimensionResult[] }) {
  if (results.length === 0) {
    return <p className="text-[12.5px] text-fg-faint">Not judged yet.</p>;
  }
  return (
    <div className="space-y-3">
      {results.map((r) => (
        <div key={r.name} className="rounded-lg border border-line bg-ink-800/60 px-3 py-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[12.5px] text-fg">{r.name}</span>
            <Badge variant={r.kind === "behaviour" ? "signal" : r.kind === "validity" ? "warn" : "neutral"}>{r.kind}</Badge>
            <span className={clsx("font-mono text-[12.5px] font-semibold", TONE_TEXT[resultTone(r)])}>
              {r.graded ? `${r.value} / 10` : "not graded"}
            </span>
            <span className="font-mono text-[11px] text-fg-faint">flagged at {r.threshold}</span>
            {r.agreement !== null && (
              <span className="font-mono text-[11px] text-fg-faint">agreement {Math.round(r.agreement * 100)}%</span>
            )}
            {r.disputed && <Badge variant="warn">disputed</Badge>}
          </div>
          <div className="mt-2 space-y-2">
            {r.judges.map((j) => (
              <div key={j.model} className="border-l-2 border-line pl-2.5">
                <div className="font-mono text-[11.5px] text-fg-muted">
                  {j.model} ·{" "}
                  {j.mean === null ? <span className="text-warn">no answer</span> : <span className="text-fg">{j.scores.join(", ")}</span>}
                </div>
                {j.error && <p className="mt-0.5 text-[12px] text-warn">{j.error}</p>}
                {j.justification && <p className="mt-0.5 text-[12.5px] leading-relaxed text-fg-muted">{j.justification}</p>}
                {j.highlights.map((h, i) => (
                  <p key={i} className="mt-1 text-[12px] text-fg-faint">
                    <span className="font-mono text-info">{h.message_id}</span> &ldquo;{h.quote}&rdquo;
                  </p>
                ))}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
