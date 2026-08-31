"use client";

// Cross-metric meta-analysis ("Analyze") — lives on the Trends page. Picks an agent, runs the
// backend meta-analysis (Spearman correlations + z-score outliers computed in Python, synthesized
// by an LLM), and renders patterns / correlations / outliers / recommendations. Self-contained
// client component: loads agents + the latest stored analysis on its own, re-runs on demand, and
// can export the result as Markdown.

import { useCallback, useEffect, useState } from "react";

import { AgentPicker } from "@/app/components/AgentPicker";
import { DocLink } from "@/app/components/DocLink";
import { Badge } from "@/app/components/ui";

type AgentRef = { id: string; slug: string; display_name: string };
type Pattern = {
  description: string;
  correlation_strength: number | null;
  evidence: string;
  affected_metrics: string[];
};
type Correlation = {
  metric_a: string;
  metric_b: string;
  coefficient: number;
  p_value: number | null;
  n: number;
  interpretation: string;
};
type Outlier = {
  conversation_id: string;
  metrics_affected: string[];
  z_scores: Record<string, number>;
  severity: string;
  reason: string;
};
type MetricStat = { metric: string; n: number; mean: number; std: number; min: number; max: number };
type MetricShift = { metric: string; prev_mean: number; mean: number; delta: number; n: number; prev_n: number };
type Result = {
  metric_stats?: MetricStat[];
  metric_shifts?: MetricShift[];
  patterns: Pattern[];
  correlations: Correlation[];
  outliers: Outlier[];
  recommendations: string[];
  summary: string;
  confidence: number;
  metrics_analyzed: number;
  conversations_analyzed: number;
};
type Analysis = {
  id: string;
  agent_id: string;
  agent_slug: string;
  status: string;
  result: Result;
  meta: { model?: string; llm?: boolean };
  created_at: string | null;
};

const ALL = "all";

function severityVariant(s: string) {
  if (s === "high") return "fail" as const;
  if (s === "medium") return "warn" as const;
  return "info" as const;
}

// For scores, up is good; for latency / tool errors, down is. `ops.turns` is neutral.
const LOWER_IS_BETTER = new Set(["ops.latency_ms", "ops.tool_errors"]);
function shiftClass(m: MetricShift) {
  if (m.delta === 0 || m.metric === "ops.turns") return "text-fg-muted";
  const improved = LOWER_IS_BETTER.has(m.metric) ? m.delta < 0 : m.delta > 0;
  return improved ? "text-ok" : "text-fail";
}
const fmtNum = (v: number) => (Math.abs(v) >= 100 ? Math.round(v).toLocaleString() : v.toFixed(2).replace(/\.?0+$/, ""));

function coefClass(c: number) {
  const a = Math.abs(c);
  if (a >= 0.7) return c >= 0 ? "text-ok" : "text-fail";
  if (a >= 0.4) return c >= 0 ? "text-ok/80" : "text-fail/80";
  return "text-fg-muted";
}

export function MetaAnalysisPanel() {
  const [agents, setAgents] = useState<AgentRef[]>([]);
  const [agentId, setAgentId] = useState<string>(ALL);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/meta-analyses/agents")
      .then((r) => (r.ok ? r.json() : []))
      .then((a) => setAgents(Array.isArray(a) ? a : []))
      .catch(() => setAgents([]));
  }, []);

  const loadLatest = useCallback((agent: string) => {
    setLoading(true);
    setError(null);
    fetch(`/api/meta-analyses/latest?agent_id=${encodeURIComponent(agent)}`)
      .then((r) => (r.ok ? r.json() : { analysis: null }))
      .then((d) => setAnalysis(d?.analysis ?? null))
      .catch(() => setAnalysis(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    loadLatest(agentId);
  }, [agentId, loadLatest]);

  const run = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      const r = await fetch("/api/meta-analyses/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_id: agentId === ALL ? "" : agentId }),
      });
      const d = await r.json();
      if (!r.ok) {
        setError(d?.detail ?? "Analysis failed.");
        return;
      }
      setAnalysis(d as Analysis);
    } catch {
      setError("Could not reach the analysis service.");
    } finally {
      setRunning(false);
    }
  }, [agentId]);

  const res = analysis?.result;

  return (
    <section className="reveal card p-5" style={{ animationDelay: "190ms" }}>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="flex items-center gap-2 text-[13px] font-semibold text-fg">
            <Spark /> Cross-metric analysis
            <DocLink path="/product/trends#cross-metric-analysis" />
          </h2>
          <p className="mt-1 text-[12.5px] text-fg-muted">
            How an agent&apos;s metrics move together — correlations, outlier conversations, and
            recommendations across all its evaluations.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <AgentPicker
            agents={agents}
            value={agentId === ALL ? "" : agentId}
            onChange={(v) => setAgentId(v || ALL)}
            allLabel="All agents"
            id="meta-agent"
            className="w-48 rounded-lg border border-line bg-ink-900 px-2.5 py-1.5 text-[12.5px] text-fg placeholder:text-fg-faint outline-none focus:border-signal/50"
          />
          <button
            onClick={run}
            disabled={running}
            className="inline-flex items-center gap-2 rounded-lg border border-signal/40 bg-signal/15 px-3.5 py-1.5 text-[12.5px] font-medium text-signal transition-all hover:bg-signal/25 hover:shadow-glow disabled:opacity-60"
          >
            {running && <Spinner />}
            {running ? "Analyzing…" : analysis ? "Re-run" : "Run analysis"}
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-fail/25 bg-fail/10 px-3.5 py-2.5 text-[12.5px] text-fail">
          {error}
        </div>
      )}

      {loading && !analysis ? (
        <Skeleton />
      ) : !res ? (
        !error && <Empty />
      ) : (
        <div className="space-y-5">
          {/* headline stats + summary */}
          <div className="grid gap-3 sm:grid-cols-3">
            <MiniStat label="Metrics" value={res.metrics_analyzed} />
            <MiniStat label="Conversations" value={res.conversations_analyzed} />
            <MiniStat
              label="Confidence"
              value={`${Math.round((res.confidence ?? 0) * 100)}%`}
              accent={res.confidence >= 0.66 ? "text-ok" : res.confidence >= 0.33 ? "text-warn" : "text-fail"}
            />
          </div>
          {res.summary && (
            <p className="text-[13.5px] leading-relaxed text-fg-muted">{res.summary}</p>
          )}

          {(res.metric_shifts?.length ?? 0) > 0 && (
            <Block title="Since last run">
              <div className="overflow-hidden rounded-lg border border-line">
                <table className="w-full text-[12.5px]">
                  <thead className="bg-hilite/[0.03] text-fg-faint">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium">Metric</th>
                      <th className="px-3 py-2 text-right font-medium">Previous mean</th>
                      <th className="px-3 py-2 text-right font-medium">Now</th>
                      <th className="px-3 py-2 text-right font-medium">Δ</th>
                    </tr>
                  </thead>
                  <tbody>
                    {res.metric_shifts!.map((m) => (
                      <tr key={m.metric} className="border-t border-line/60">
                        <td className="px-3 py-2 font-mono text-[11.5px] text-fg">{m.metric}</td>
                        <td className="px-3 py-2 text-right font-mono tabular-nums text-fg-faint">{fmtNum(m.prev_mean)}</td>
                        <td className="px-3 py-2 text-right font-mono tabular-nums text-fg">{fmtNum(m.mean)}</td>
                        <td className={`px-3 py-2 text-right font-mono tabular-nums ${shiftClass(m)}`}>
                          {m.delta > 0 ? "+" : ""}{fmtNum(m.delta)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Block>
          )}

          {res.patterns.length > 0 && (
            <Block title="Patterns">
              <div className="space-y-2.5">
                {res.patterns.map((p, i) => (
                  <div key={i} className="rounded-lg border border-line bg-hilite/[0.02] p-3">
                    <div className="text-[13px] text-fg">{p.description}</div>
                    {p.evidence && (
                      <div className="mt-1 text-[12px] text-fg-faint">{p.evidence}</div>
                    )}
                    {p.affected_metrics.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {p.affected_metrics.map((m) => (
                          <Chip key={m}>{m}</Chip>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </Block>
          )}

          {res.correlations.length > 0 && (
            <Block title="Correlations">
              <div className="overflow-hidden rounded-lg border border-line">
                <table className="w-full text-[12.5px]">
                  <thead className="bg-hilite/[0.03] text-fg-faint">
                    <tr>
                      <th className="px-3 py-2 text-left font-medium">Metric pair</th>
                      <th className="px-3 py-2 text-right font-medium">Spearman</th>
                      <th className="px-3 py-2 text-right font-medium">n</th>
                      <th className="px-3 py-2 text-left font-medium">Interpretation</th>
                    </tr>
                  </thead>
                  <tbody>
                    {res.correlations.map((c, i) => (
                      <tr key={i} className="border-t border-line/60">
                        <td className="px-3 py-2 font-mono text-[11.5px] text-fg">
                          {c.metric_a} <span className="text-fg-faint">↔</span> {c.metric_b}
                        </td>
                        <td
                          className={`px-3 py-2 text-right font-mono tabular-nums ${coefClass(c.coefficient)}`}
                        >
                          {c.coefficient >= 0 ? "+" : ""}
                          {c.coefficient.toFixed(2)}
                        </td>
                        <td className="px-3 py-2 text-right font-mono tabular-nums text-fg-faint">
                          {c.n}
                        </td>
                        <td className="px-3 py-2 text-fg-muted">{c.interpretation}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Block>
          )}

          {res.outliers.length > 0 && (
            <Block title="Outlier conversations">
              <div className="space-y-2">
                {res.outliers.map((o) => (
                  <div
                    key={o.conversation_id}
                    className="flex items-start justify-between gap-3 rounded-lg border border-line bg-hilite/[0.02] p-3"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <Badge variant={severityVariant(o.severity)} dot>
                          {o.severity}
                        </Badge>
                        <a
                          href={`/sessions/${encodeURIComponent(o.conversation_id)}`}
                          className="truncate font-mono text-[11.5px] text-signal hover:underline"
                        >
                          {o.conversation_id}
                        </a>
                      </div>
                      <div className="mt-1.5 text-[12.5px] text-fg-muted">{o.reason}</div>
                    </div>
                    <div className="flex shrink-0 flex-wrap justify-end gap-1.5">
                      {Object.entries(o.z_scores).map(([m, z]) => (
                        <Chip key={m} title={`${m}: z=${z}`}>
                          {m} <span className="text-fg-faint">z={z}</span>
                        </Chip>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </Block>
          )}

          {res.recommendations.length > 0 && (
            <Block title="Recommendations">
              <ul className="space-y-1.5">
                {res.recommendations.map((r, i) => (
                  <li key={i} className="flex gap-2 text-[13px] text-fg-muted">
                    <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-signal" />
                    {r}
                  </li>
                ))}
              </ul>
            </Block>
          )}

          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line/60 pt-3 text-[11.5px] text-fg-faint">
            <span>
              {analysis?.created_at && `Generated ${new Date(analysis.created_at).toLocaleString()}`}
              {analysis?.meta?.model ? ` · ${analysis.meta.model}` : analysis?.meta?.llm === false ? " · stats only" : ""}
            </span>
            <button
              onClick={() => exportMarkdown(analysis!)}
              className="rounded-md border border-line px-2.5 py-1 text-fg-muted transition-colors hover:border-signal/40 hover:text-fg"
            >
              Export Markdown
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h3 className="mb-2 font-mono text-[10.5px] uppercase tracking-[0.16em] text-fg-faint">
        {title}
      </h3>
      {children}
    </div>
  );
}

function MiniStat({ label, value, accent }: { label: string; value: React.ReactNode; accent?: string }) {
  return (
    <div className="rounded-lg border border-line bg-hilite/[0.02] px-3.5 py-2.5">
      <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-fg-faint">{label}</div>
      <div className={`mt-1 font-display text-[22px] font-bold tabular-nums ${accent ?? "text-fg"}`}>
        {value}
      </div>
    </div>
  );
}

function Chip({ children, title }: { children: React.ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className="inline-flex items-center gap-1 rounded-md border border-line bg-hilite/[0.04] px-1.5 py-0.5 font-mono text-[10.5px] text-fg-muted"
    >
      {children}
    </span>
  );
}

function Empty() {
  return (
    <div className="rounded-lg border border-dashed border-line px-4 py-8 text-center">
      <p className="text-[13px] text-fg-muted">No analysis yet for this agent.</p>
      <p className="mt-1 text-[12px] text-fg-faint">
        Run it to surface cross-metric correlations, outlier conversations, and recommendations from
        your evaluation scores.
      </p>
    </div>
  );
}

function Skeleton() {
  return (
    <div className="space-y-3">
      <div className="h-16 animate-pulse rounded-lg bg-hilite/[0.03]" />
      <div className="h-4 w-3/4 animate-pulse rounded bg-hilite/[0.03]" />
      <div className="h-24 animate-pulse rounded-lg bg-hilite/[0.03]" />
    </div>
  );
}

function Spinner() {
  return <span className="h-3 w-3 animate-spin rounded-full border-2 border-signal/30 border-t-signal" />;
}

function Spark() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" className="text-signal">
      <path
        d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M18 6l-2.5 2.5M8.5 15.5L6 18"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  );
}

function exportMarkdown(a: Analysis) {
  const r = a.result;
  const lines: string[] = [];
  lines.push(`# Meta-analysis${a.agent_slug ? ` — ${a.agent_slug}` : ""}`);
  if (a.created_at) lines.push(`_Generated ${new Date(a.created_at).toLocaleString()}_`);
  lines.push("");
  lines.push(
    `**Metrics:** ${r.metrics_analyzed} · **Conversations:** ${r.conversations_analyzed} · **Confidence:** ${Math.round(
      (r.confidence ?? 0) * 100,
    )}%`,
  );
  lines.push("");
  if (r.summary) {
    lines.push("## Summary", r.summary, "");
  }
  if (r.patterns.length) {
    lines.push("## Patterns");
    for (const p of r.patterns) {
      lines.push(`- ${p.description}${p.evidence ? ` _(${p.evidence})_` : ""}`);
    }
    lines.push("");
  }
  if (r.metric_shifts?.length) {
    lines.push("## Since last run", "", "| Metric | Previous mean | Now | Δ |", "| --- | --- | --- | --- |");
    for (const m of r.metric_shifts) {
      lines.push(`| ${m.metric} | ${m.prev_mean} | ${m.mean} | ${m.delta > 0 ? "+" : ""}${m.delta} |`);
    }
    lines.push("");
  }
  if (r.correlations.length) {
    lines.push("## Correlations", "", "| Metric A | Metric B | Spearman | n | Interpretation |", "| --- | --- | --- | --- | --- |");
    for (const c of r.correlations) {
      lines.push(
        `| ${c.metric_a} | ${c.metric_b} | ${c.coefficient >= 0 ? "+" : ""}${c.coefficient.toFixed(2)} | ${c.n} | ${c.interpretation} |`,
      );
    }
    lines.push("");
  }
  if (r.outliers.length) {
    lines.push("## Outlier conversations");
    for (const o of r.outliers) {
      lines.push(`- **[${o.severity}]** ${o.conversation_id} — ${o.reason}`);
    }
    lines.push("");
  }
  if (r.recommendations.length) {
    lines.push("## Recommendations");
    for (const rec of r.recommendations) lines.push(`- ${rec}`);
    lines.push("");
  }
  const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `meta-analysis-${a.agent_slug || "project"}.md`;
  link.click();
  URL.revokeObjectURL(url);
}
