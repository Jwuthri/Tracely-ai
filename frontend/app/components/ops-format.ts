// Display + costing helpers for the ops roll-up. Split out of OpsPanel.tsx purely so tests can
// import them — OpsPanel pulls in `api.ts` → `server-only`, which vitest can't load.
import type { Ops } from "@/app/lib/api";
import { rateFor } from "@/app/lib/usage";

/** Durations are always ms on the wire; render the unit a human would use. A 0 here is a real
 *  sub-millisecond measurement (ClickHouse rounds to ms), not missing data — hence "<1ms". */
export function dur(ms: number): string {
  if (ms <= 0) return "<1ms";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

/** TTFT is only measurable when the span carried `completion_start_time`; 0 means never reported. */
export const ttft = (ms: number) => (ms > 0 ? dur(ms) : "—");

export function tokens(n: number): string {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1)}B`; // "8310.8M" read as a typo
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toLocaleString();
}

export const pct = (x: number) => `${(x * 100).toFixed(x < 0.1 ? 1 : 0)}%`;

const wholeUsd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const centsUsd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

/** Money for a tile or table cell. `fmtUsd` (usage.ts) prints 4–6 decimals — right for one span,
 *  absurd for a window total ("$15680.2080"). Here precision falls as the number grows: cents up to
 *  $1k, whole dollars after, "$2.40M" past a million. A real sub-cent amount stays distinct from 0. */
export function usd(n: number): string {
  if (!(n > 0)) return "$0";
  if (n < 0.01) return "<$0.01";
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  return (n >= 1000 ? wholeUsd : centsUsd).format(n);
}

/** Reported cost when the SDK sent one, else list price from the token split — same rate table
 *  the trace pages use, so a model row here matches the header on the trace it came from. */
export function modelCost(m: Ops["models"][number]): number {
  if (m.cost > 0) return m.cost;
  const [ci, co] = rateFor(m.model);
  return (m.input_tokens * ci + m.output_tokens * co) / 1_000_000;
}

/** Window cost: what the SDK reported, else the sum of per-model list price. */
export function totalCost(ops: Pick<Ops, "summary" | "models">): number {
  return ops.summary.cost > 0 ? ops.summary.cost : ops.models.reduce((a, m) => a + modelCost(m), 0);
}
