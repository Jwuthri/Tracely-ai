import clsx from "clsx";
import Link from "next/link";
import { notFound } from "next/navigation";
import { AuditRunView } from "@/app/components/AuditRunView";
import { GateAutoRefresh } from "@/app/components/GateAutoRefresh";
import { IconArrowLeft } from "@/app/components/icons";
import { TimeAgo } from "@/app/components/TimeAgo";
import { Badge } from "@/app/components/ui";
import { getAuditRun } from "@/app/lib/api";
import { formatRate, formatUsd, verdictTone } from "@/app/lib/audits";

export default async function AuditRunPage({
  params,
}: {
  params: Promise<{ auditId: string; runId: string }>;
}) {
  const { auditId, runId } = await params;
  const run = await getAuditRun(runId);
  if (!run || run.audit_id !== auditId) notFound();

  const running = run.status === "RUNNING";
  const s = run.summary ?? {};
  const rate = s.elicitation_rate;
  const verdicts = Object.entries(s.verdicts ?? {});
  const excluded = (s.total ?? 0) - (s.counted ?? 0);

  return (
    <div className="space-y-6">
      <header className="reveal">
        <Link href={`/audits/${auditId}`} className="inline-flex items-center gap-1.5 font-mono text-[11.5px] text-fg-faint transition-colors hover:text-signal">
          <IconArrowLeft className="h-3.5 w-3.5" /> {run.audit.name || "Audit"}
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="font-display text-[26px] font-extrabold tracking-tight">Run</h1>
          <TimeAgo ts={run.created_at} className="font-mono text-[12px] text-fg-faint" />
          {running && <GateAutoRefresh label={`running — ${run.done} of ${run.total} rollouts done`} />}
          {run.status === "CANCELLED" && <Badge variant="neutral">cancelled</Badge>}
          {run.mode === "explore" && (
            <Badge variant="info" title="Rollouts learn from each other in this mode, so its rate is not comparable with a measure run">
              explore
            </Badge>
          )}
        </div>
      </header>

      <div className="reveal grid gap-4 sm:grid-cols-[minmax(220px,1fr)_2fr]" style={{ animationDelay: "60ms" }}>
        <div className="card px-5 py-4">
          <div className="font-mono text-[10px] uppercase tracking-wider text-fg-faint">Elicitation rate</div>
          <div className={clsx("mt-1 font-display text-[44px] font-extrabold leading-none tracking-tight",
            running || rate == null ? "text-fg-faint" : rate > 0 ? "text-fail" : "text-ok")}>
            {running ? "…" : formatRate(rate)}
          </div>
          <p className="mt-2 text-[12.5px] leading-relaxed text-fg-muted">
            {running
              ? "Computed when every rollout is judged."
              : s.status === "NO_COVERAGE"
                ? "No rollout was both valid and judged, so there is no rate. That is not the same as 0%."
                : `Over the ${s.counted} rollout${s.counted === 1 ? " that was" : "s that were"} a fair test` +
                  (excluded > 0 ? `; ${excluded} more ${excluded === 1 ? "was" : "were"} excluded.` : ".")}
          </p>
        </div>

        <div className="card grid grid-cols-2 divide-x divide-line/60 sm:grid-cols-4">
          <Meta k="Target" v={run.config.target.model} />
          <Meta k="Spent" v={`${formatUsd(run.spend_usd)} of ${formatUsd(run.budget_usd)}`} />
          <Meta k="Judges" v={`${run.config.judge_models?.length ?? 1} per dimension`} />
          <Meta k="Attacker turns" v={`≤ ${run.config.attacker.max_turns}`} />
          <div className="col-span-2 flex flex-wrap items-center gap-1.5 border-t border-line/60 px-4 py-3 sm:col-span-4">
            {verdicts.length === 0 ? (
              <span className="font-mono text-[11.5px] text-fg-faint">No rollout has finished yet.</span>
            ) : (
              verdicts.map(([v, n]) => (
                <Badge key={v} variant={verdictTone(v)}>{n} {v.toLowerCase()}</Badge>
              ))
            )}
          </div>
        </div>
      </div>

      <div className="reveal" style={{ animationDelay: "120ms" }}>
        <AuditRunView run={run} />
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
