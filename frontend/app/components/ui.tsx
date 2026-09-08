import clsx from "clsx";
import type { ReactNode } from "react";

type Variant = "ok" | "fail" | "warn" | "info" | "signal" | "neutral";

const BADGE: Record<Variant, string> = {
  ok: "bg-ok/10 text-ok border-ok/25",
  fail: "bg-fail/10 text-fail border-fail/25",
  warn: "bg-warn/10 text-warn border-warn/25",
  info: "bg-info/10 text-info border-info/25",
  signal: "bg-signal/10 text-signal border-signal/25",
  neutral: "bg-hilite/[0.04] text-fg-muted border-line",
};
const DOT: Record<Variant, string> = {
  ok: "bg-ok",
  fail: "bg-fail",
  warn: "bg-warn",
  info: "bg-info",
  signal: "bg-signal",
  neutral: "bg-fg-faint",
};

export function Badge({
  variant = "neutral",
  children,
  dot = false,
  className,
  title,
}: {
  variant?: Variant;
  children: ReactNode;
  dot?: boolean;
  className?: string;
  /** Native tooltip — the one-line "what this badge actually establishes". */
  title?: string;
}) {
  return (
    <span
      title={title}
      className={clsx(
        "inline-flex items-center gap-1.5 rounded-md border px-2 py-[3px] font-mono text-[10.5px] font-semibold uppercase tracking-wide",
        BADGE[variant],
        className,
      )}
    >
      {dot && <span className={clsx("h-1.5 w-1.5 rounded-full", DOT[variant])} />}
      {children}
    </span>
  );
}

export function verdictVariant(v?: string | null): Variant {
  if (v === "PASS") return "ok";
  if (v === "FAIL" || v === "ERROR") return "fail";
  // UNGRADED and NO_COVERAGE block the merge — rendering them the same grey as SKIP/PENDING
  // made a blocking gate look idle.
  // INCOMPLETE: a required check could not run — blocking, and not a failure either.
  if (v === "UNGRADED" || v === "NO_COVERAGE" || v === "INCOMPLETE") return "warn";
  return "neutral";
}

export function statusVariant(s: string): Variant {
  if (s === "PROMOTED") return "ok";
  if (s === "DRAFT") return "warn";
  if (s === "QUARANTINED" || s === "UNREPRODUCIBLE") return "fail";
  return "neutral";
}

const TYPE: Record<string, string> = {
  AGENT: "text-t_agent",
  SUBAGENT: "text-t_agent",
  GENERATION: "text-t_llm",
  EMBEDDING: "text-t_llm",
  TOOL: "text-t_tool",
  RETRIEVER: "text-t_retriever",
  THINKING: "text-t_think",
  DELEGATE: "text-t_delegate",
  SKILL: "text-t_skill",
};
const TYPE_DOT: Record<string, string> = {
  AGENT: "bg-t_agent",
  SUBAGENT: "bg-t_agent",
  GENERATION: "bg-t_llm",
  EMBEDDING: "bg-t_llm",
  TOOL: "bg-t_tool",
  RETRIEVER: "bg-t_retriever",
  THINKING: "bg-t_think",
  DELEGATE: "bg-t_delegate",
  SKILL: "bg-t_skill",
};

// Canonicalize synonym types for display + filtering. Mirrors backend/tracely/otel/mapping.py:
// _TYPE_ALIASES, so backfilled REASONING rows render & filter together with THINKING.
const TYPE_ALIASES: Record<string, string> = { REASONING: "THINKING" };
export function normalizeType(type: string | null | undefined): string {
  const up = (type || "STEP").toUpperCase();
  return TYPE_ALIASES[up] ?? up;
}

export function TypeChip({ type, className }: { type: string; className?: string }) {
  const t = normalizeType(type);
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 font-mono text-[10px] font-semibold uppercase tracking-wider",
        TYPE[t] ?? "text-t_step",
        className,
      )}
    >
      <span className={clsx("h-2 w-2 rounded-[3px]", TYPE_DOT[t] ?? "bg-t_step")} />
      {t}
    </span>
  );
}

export function StatCard({
  label,
  value,
  sub,
  accent,
  chart,
  delay = 0,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  accent?: string;
  chart?: ReactNode; // optional sparkline under the number — a count alone never shows direction
  delay?: number;
}) {
  return (
    <div className="reveal card p-5" style={{ animationDelay: `${delay}ms` }}>
      <div className="font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-faint">{label}</div>
      <div
        className={clsx(
          "mt-2.5 font-display text-[36px] font-extrabold leading-none tracking-tight tabular-nums",
          accent ?? "text-fg",
        )}
      >
        {value}
      </div>
      {chart != null && <div className="mt-3">{chart}</div>}
      {sub != null && <div className="mt-2.5 text-[12.5px] text-fg-muted">{sub}</div>}
    </div>
  );
}
