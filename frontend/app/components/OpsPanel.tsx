import { getOps, type Ops } from "@/app/lib/api";
import { StatCard } from "@/app/components/ui";
import { Spark, Legend } from "@/app/components/Bars";
import { dur, modelCost, pct, tokens, totalCost, ttft, usd } from "@/app/components/ops-format";

export async function OpsPanel({ days = 14 }: { days?: number }) {
  let ops: Ops;
  try {
    ops = await getOps(days);
  } catch {
    return null; // never block Trends on the ops roll-up
  }
  const s = ops.summary;
  const cost = totalCost(ops);
  // Daily spend as the tile's sparkline: a total alone can't say whether spend is climbing. Under
  // three days there is no shape, and a one-point line just renders as a block.
  const spend = ops.daily.map((d) => d.cost);
  // Zero cost next to LLM calls means "no list price for these models", not "free" — the backend
  // leaves cost_details empty for an unpriced model on purpose — so say that instead of "$0".
  const costSub =
    cost > 0 && s.traces
      ? `${usd((cost / s.traces) * 1000)} per 1k traces`
      : s.llm_calls
        ? "no list price for these models"
        : "no LLM calls in this window";

  return (
    <>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="Latency (p50)" value={dur(s.p50)} sub="median trace, end-to-end" delay={40} />
        <StatCard label="Latency (p95)" value={dur(s.p95)} accent="text-warn" sub="slow tail" delay={70} />
        <StatCard label="Latency (p99)" value={dur(s.p99)} accent="text-fail" sub="worst 1%" delay={100} />
        <StatCard
          label="Time to first token"
          value={ttft(s.ttft_p50)}
          accent="text-signal"
          sub={s.ttft_p50 > 0 ? "p50 · streaming spans" : "not reported by this SDK"}
          delay={130}
        />
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="Throughput" value={s.traces_per_day} sub={`traces / day · ${s.traces} total`} delay={40} />
        <StatCard
          label="Error rate"
          value={pct(s.error_rate)}
          accent={s.errors ? "text-fail" : "text-ok"}
          sub={`${s.errors} traces with an ERROR span`}
          delay={70}
        />
        <StatCard label="Tokens" value={tokens(s.tokens)} sub={`${s.llm_calls.toLocaleString()} LLM calls`} delay={100} />
        <StatCard
          label="Cost"
          value={cost > 0 ? usd(cost) : "—"}
          sub={costSub}
          chart={
            cost > 0 && spend.length >= 3 ? (
              <Spark series={[{ values: spend, stroke: "stroke-warn/80", fill: "fill-warn/10", label: "cost" }]} height={30} grid={false} />
            ) : undefined
          }
          delay={130}
        />
      </div>

      <section className="reveal card p-5" style={{ animationDelay: "150ms" }}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-[13px] font-semibold text-fg">Latency over time</h2>
          <span className="font-mono text-[10px] uppercase tracking-wider text-fg-faint">
            per day · peak {dur(Math.max(0, ...ops.daily.map((d) => d.p99)))}
          </span>
        </div>
        <Spark
          series={[
            { values: ops.daily.map((d) => d.p99), stroke: "stroke-fail/70", label: "p99" },
            { values: ops.daily.map((d) => d.p95), stroke: "stroke-warn/70", label: "p95" },
            { values: ops.daily.map((d) => d.p50), stroke: "stroke-signal", fill: "fill-signal/10", label: "p50" },
          ]}
        />
        <div className="mt-2 flex justify-between font-mono text-[9.5px] text-fg-faint">
          <span>{ops.daily[0]?.date.slice(5) ?? ""}</span>
          <span>{ops.daily.at(-1)?.date.slice(5) ?? ""}</span>
        </div>
        <Legend items={[["bg-signal", "p50"], ["bg-warn/70", "p95"], ["bg-fail/70", "p99"]]} />
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <Table
          title="By model"
          note="latency of the generation span itself"
          cols={["Model", "Calls", "p50", "p95", "TTFT", "Tokens", "Cost"]}
          rows={ops.models.map((m) => [
            m.model,
            m.calls.toLocaleString(),
            dur(m.p50),
            dur(m.p95),
            ttft(m.ttft_p50),
            tokens(m.tokens),
            modelCost(m) > 0 ? usd(modelCost(m)) : "—", // no list price ≠ free
          ])}
          empty="No model spans in this window."
        />
        <Table
          title="Slowest operations"
          note="by p95, across every span"
          cols={["Operation", "Type", "Calls", "p50", "p95", "Max", "Errors"]}
          rows={ops.slowest.map((o) => [
            o.name,
            o.type.toLowerCase(),
            o.calls.toLocaleString(),
            dur(o.p50),
            dur(o.p95),
            dur(o.max),
            o.errors ? String(o.errors) : "0",
          ])}
          empty="No spans in this window."
        />
      </div>
    </>
  );
}

/** Shared shape for both roll-ups: first column is a left-aligned name, the rest are right-aligned
 *  numbers. Deliberately dumb (string cells, no sorting) — these are read-only summaries. */
function Table({
  title,
  note,
  cols,
  rows,
  empty,
}: {
  title: string;
  note: string;
  cols: string[];
  rows: string[][];
  empty: string;
}) {
  return (
    <section className="reveal card p-5" style={{ animationDelay: "170ms" }}>
      <div className="mb-4 flex items-center justify-between gap-3">
        <h2 className="text-[13px] font-semibold text-fg">{title}</h2>
        <span className="font-mono text-[10px] uppercase tracking-wider text-fg-faint">{note}</span>
      </div>
      {rows.length === 0 ? (
        <div className="py-8 text-center text-[12.5px] text-fg-faint">{empty}</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[12.5px]">
            <thead>
              <tr className="border-b border-line text-left font-mono text-[10px] uppercase tracking-wider text-fg-faint">
                {cols.map((c, i) => (
                  <th key={c} className={`px-2 py-2 font-medium ${i ? "text-right" : ""}`}>
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r[0]} className="border-b border-line/40 last:border-0 hover:bg-hilite/[0.015]">
                  {r.map((cell, i) => (
                    <td
                      key={i}
                      className={`px-2 py-2 font-mono tabular-nums ${
                        i ? "text-right text-fg-muted" : "max-w-[16ch] truncate text-fg"
                      }`}
                      title={i === 0 ? cell : undefined}
                    >
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
