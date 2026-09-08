import { DocLink } from "@/app/components/DocLink";
import { getCases, getClusters, getEvaluators, getGates, getMilestones, getStats, getTraces, getTrends, type EvalCase, type FailureCluster, type GateRun } from "@/app/lib/api";
import { getMe } from "@/app/lib/auth";
import { Activation } from "@/app/components/Activation";
import { Badge, StatCard, statusVariant, verdictVariant } from "@/app/components/ui";
import { IconChevron } from "@/app/components/icons";
import { OpsStrip } from "@/app/components/OpsPanel";
import { Spark } from "@/app/components/Bars";
import { ClusterMeter, TaxonomyChip, clusterTone, compactCount } from "@/app/components/ClusterMeter";
import { IgnoreCluster } from "@/app/components/IgnoreCluster";
import { RowLink } from "@/app/components/RowLink";
import { TimeAgo } from "@/app/components/TimeAgo";

function SectionHead({ title, href }: { title: string; href: string }) {
  return (
    <div className="flex items-center justify-between border-b border-line px-4 py-3">
      <h2 className="text-[13.5px] font-semibold text-fg">{title}</h2>
      <a href={href} className="flex items-center gap-0.5 text-[12px] text-fg-muted transition-colors hover:text-signal">
        View all <IconChevron className="h-3.5 w-3.5" />
      </a>
    </div>
  );
}

/** A trace is red if it errored or an evaluator failed it, green once something graded it, and
 *  grey while it is still unevaluated — a grey dot is "no verdict yet", not "passed". */
function dotColor(t: { has_error?: number; eval?: string | null }): string {
  if (t.has_error || t.eval === "FAIL") return "bg-fail";
  return t.eval === "PASS" ? "bg-ok" : "bg-fg-faint/40";
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="px-4 py-10 text-center text-[13px] text-fg-faint">{children}</div>;
}

/** Top open clusters as count-proportional meters — the dashboard's "what should I work on
 *  next". A cluster is already a ranked pile of identical failures, so the meter IS the
 *  priority; the colour is the failure family, so the pile is legible before it is read. */
function TopClusters({ clusters }: { clusters: FailureCluster[] }) {
  // "OPEN" is the only untriaged state — a cluster becomes "PROMOTED" once it has a regression
  // case, at which point it's handled and no longer work to pick up (see repositories.py).
  const top = clusters
    .filter((c) => c.status === "OPEN")
    .sort((a, b) => b.count - a.count)
    .slice(0, 5);
  if (top.length === 0) return null;

  const max = Math.max(1, ...top.map((c) => c.count));
  return (
    <section className="reveal card overflow-hidden" style={{ animationDelay: "200ms" }}>
      <SectionHead title="Biggest failure clusters" href="/clusters" />
      <div className="space-y-1 p-2">
        {top.map((c) => {
          const tone = clusterTone(c.taxonomy);
          return (
            <RowLink
              key={c.id}
              href={`/clusters/${c.id}`}
              className="group flex gap-3.5 rounded-lg px-2.5 py-2.5 transition-colors hover:bg-hilite/[0.03]"
            >
              <span className="flex w-10 shrink-0 flex-col items-end pt-px leading-none" title={`${c.count} traces`}>
                <span className={`font-display text-[21px] font-extrabold tabular-nums ${tone.text}`}>
                  {compactCount(c.count)}
                </span>
                <span className="mt-1 font-mono text-[9px] uppercase tracking-wider text-fg-faint">
                  {c.count === 1 ? "trace" : "traces"}
                </span>
              </span>
              <span className="flex min-w-0 flex-1 flex-col gap-2">
                {/* clamped to two lines, not one: these labels are LLM-written sentences, and
                    truncating every one at the same column makes five rows look identical. */}
                <span className="line-clamp-2 text-[13px] leading-snug text-fg-muted transition-colors group-hover:text-fg">
                  {c.label || c.signature}
                </span>
                <span className="flex items-center gap-2.5">
                  <ClusterMeter value={c.count} max={max} tone={tone} className="min-w-0 flex-1" />
                  <TaxonomyChip taxonomy={c.taxonomy ?? ""} tone={tone} />
                </span>
              </span>
              <IgnoreCluster clusterId={c.id} />
            </RowLink>
          );
        })}
      </div>
    </section>
  );
}

function NextActions({ clusters, cases, gate }: { clusters: FailureCluster[]; cases: EvalCase[]; gate: GateRun | null }) {
  const unprotected = clusters.filter((c) => c.status === "OPEN" && !c.candidate_case_id);
  const incomplete = cases.filter((c) => c.status === "DRAFT" || !c.verified_candidate_trace_id || c.verified_case_version !== (c.version ?? 1));
  const blocked = gate && gate.status !== "PASS" && gate.status !== "RUNNING";
  if (unprotected.length === 0 && incomplete.length === 0 && !gate) return null;
  return (
    <section className="reveal grid grid-cols-1 gap-3 sm:grid-cols-3" style={{ animationDelay: "160ms" }} aria-label="Next actions">
      <Tile
        n={unprotected.length}
        label="unprotected failures"
        sub="open clusters with no regression case"
        tone={unprotected.length ? "text-warn" : "text-fg"}
        href={unprotected[0] ? `/clusters/${unprotected[0].id}` : "/clusters"}
        cta={unprotected[0] ? "Promote the biggest →" : "Failure clusters →"}
      />
      <Tile
        n={incomplete.length}
        label="incomplete tests"
        sub="draft, or no verified fix at the current version"
        tone={incomplete.length ? "text-warn" : "text-fg"}
        href={incomplete[0] ? `/cases/${incomplete[0].id}` : "/cases"}
        cta={incomplete[0] ? "Open the first →" : "Regression cases →"}
      />
      <Tile
        n={gate ? undefined : 0}
        label={gate ? `last CI run: ${gate.status}` : "no CI run yet"}
        sub={gate ? `${gate.agent ?? ""}${gate.run_id ? "" : " · not run-scoped"}` : "wire the gate into a workflow"}
        tone={blocked ? "text-fail" : gate ? "text-ok" : "text-fg"}
        href={gate ? `/gates/${gate.id}` : "https://docs.tracely-ai.com/cli"}
        cta={gate ? "Inspect the run →" : "CI setup →"}
      />
    </section>
  );
}

function Tile({ n, label, sub, tone, href, cta }: { n?: number; label: string; sub: string; tone: string; href: string; cta: string }) {
  return (
    <a href={href} className="card flex items-center justify-between gap-3 px-4 py-3 transition-colors hover:bg-hilite/[0.025]">
      <span className="min-w-0">
        <span className="flex items-baseline gap-2">
          {n !== undefined && <span className={`font-display text-[22px] font-extrabold tabular-nums ${tone}`}>{n}</span>}
          <span className={`text-[13px] font-semibold ${n === undefined ? tone : "text-fg"}`}>{label}</span>
        </span>
        <span className="block truncate font-mono text-[11px] text-fg-faint">{sub}</span>
      </span>
      <span className="shrink-0 text-[12px] text-signal">{cta}</span>
    </a>
  );
}

export default async function Dashboard() {
  // The dashboard only ever renders the top few of each list, so it asks for exactly that many
  // instead of pulling every case and cluster in the project to slice 6 off the front. The big
  // numbers above come from `getStats()`, which counts server-side.
  const [stats, traces, casesPage, trends, clustersPage, evaluators, gatesPage, me, milestones] = await Promise.all([
    getStats(),
    getTraces(),
    getCases(6),
    getTrends(14),
    getClusters(undefined, 6),
    getEvaluators(),
    getGates(1),
    getMe(),
    getMilestones(),
  ]);
  const cases = casesPage.items;
  const clusters = clustersPage.items;
  // 14-day shape behind the headline counts — a count with no direction can't tell you if it's
  // getting worse, which is the only question a dashboard number is really asked. Under three
  // days there is no shape to show, and a one-point spark just renders as a solid block.
  const spark = (values: number[], stroke: string, fill: string) =>
    values.length < 3 ? undefined : (
      <Spark series={[{ values, stroke, fill, label: "" }]} height={30} grid={false} />
    );
  const volume = trends.daily.map((d) => d.traces);
  const failures = trends.daily.map((d) => d.failures);

  return (
    <div className="space-y-8">
      <header className="reveal">
        <div className="flex items-center gap-3"><h1 className="font-display text-[27px] font-extrabold tracking-tight">Dashboard</h1><DocLink path="/product/dashboard" /></div>
        <p className="mt-1.5 text-[14px] text-fg-muted">
          Production traces become regression tests — detect a failure, promote it, gate it forever.
        </p>
      </header>

      {/* Shown until the loop has been closed once; every step reads a real count. */}
      <Activation
        traces={stats.traces}
        evaluators={evaluators.length}
        failures={stats.auto_failures}
        clusters={stats.open_clusters}
        cases={stats.cases}
        gates={gatesPage.total}
        ingestKey={me?.ingest_keys?.[0] ?? "<your-ingest-key>"}
        endpoint={process.env.NEXT_PUBLIC_TRACELY_PUBLIC_API ?? "http://localhost:8000"}
        milestones={milestones}
      />

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard
          label="Traces"
          value={stats.traces}
          sub={`${stats.spans} spans`}
          chart={spark(volume, "stroke-signal", "fill-signal/10")}
          delay={0}
        />
        <StatCard
          label="Failure clusters"
          value={stats.open_clusters}
          accent={stats.open_clusters ? "text-warn" : "text-fg"}
          sub="open · to triage"
          delay={60}
        />
        <StatCard
          label="Auto failures"
          value={stats.auto_failures}
          accent={stats.auto_failures ? "text-fail" : "text-fg"}
          sub="auto-detected, incl. silent"
          chart={spark(failures, "stroke-fail", "fill-fail/10")}
          delay={120}
        />
        <StatCard label="Regression cases" value={stats.cases} accent="text-signal" sub="forever-running" delay={180} />
      </div>

      <OpsStrip />

      {/* What needs a decision, with the action beside it — the charts below are evidence. */}
      <NextActions clusters={clusters} cases={cases} gate={gatesPage.items[0] ?? null} />

      <TopClusters clusters={clusters} />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section className="reveal card overflow-hidden" style={{ animationDelay: "220ms" }}>
          <SectionHead title="Recent traces" href="/traces" />
          {traces.length === 0 ? (
            <Empty>No traces yet — send one with the SDK or OTLP.</Empty>
          ) : (
            traces.slice(0, 6).map((t) => (
              <a
                key={t.trace_id}
                href={`/traces/${t.trace_id}`}
                className="flex items-center justify-between gap-3 border-b border-line/50 px-4 py-3 transition-colors last:border-0 hover:bg-hilite/[0.025]"
              >
                <span className="flex min-w-0 items-center gap-2.5">
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dotColor(t)}`} />
                  {/* an unnamed trace is identified by its id — the old fallback printed the
                      word "trace" on every row, which named nothing */}
                  <span className={`truncate text-[13px] ${t.root_name ? "text-fg" : "font-mono text-[12px] text-fg-muted"}`}>
                    {t.root_name || `${t.trace_id.slice(0, 12)}…`}
                  </span>
                </span>
                <span className="flex shrink-0 items-center gap-2.5 font-mono text-[11px] text-fg-faint">
                  <span>{t.spans} spans</span>
                  <TimeAgo ts={t.ts} />
                  {/* the eval verdict was in the payload all along and nothing showed it —
                      "did this trace pass" is the one thing the product exists to answer */}
                  {t.has_error ? (
                    <Badge variant="fail">error</Badge>
                  ) : t.eval ? (
                    <Badge variant={t.eval === "FAIL" ? "fail" : "ok"}>{t.eval}</Badge>
                  ) : null}
                </span>
              </a>
            ))
          )}
        </section>

        <section className="reveal card overflow-hidden" style={{ animationDelay: "280ms" }}>
          <SectionHead title="Regression cases" href="/cases" />
          {cases.length === 0 ? (
            <Empty>No cases yet — promote a failing trace.</Empty>
          ) : (
            cases.slice(0, 6).map((c) => (
              <a
                key={c.id}
                href={`/cases/${c.id}`}
                className="flex items-center justify-between border-b border-line/50 px-4 py-3 transition-colors last:border-0 hover:bg-hilite/[0.025]"
              >
                <span className="truncate text-[13px] text-fg">{c.title || "case"}</span>
                <span className="flex shrink-0 items-center gap-2">
                  {c.last_verdict && <Badge variant={verdictVariant(c.last_verdict)}>{c.last_verdict}</Badge>}
                  <Badge variant={statusVariant(c.status)} dot>
                    {c.status}
                  </Badge>
                </span>
              </a>
            ))
          )}
        </section>
      </div>
    </div>
  );
}
