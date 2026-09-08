/**
 * The evidence behind one case verdict (W2 contract): which checks were required, which ran,
 * which could not — and whether the execution completed under its declared mode. Renders
 * nothing for an old row that predates the contract (no `checks`), so legacy replays keep
 * their old one-line reason.
 */
export type CaseCheck = { name: string; required: boolean; status: string; reason?: string };
export type CaseExecution = { mode?: string; complete?: boolean; problem?: string; diverged?: string[] };

export function checksOf(detail: unknown): { checks: CaseCheck[]; execution: CaseExecution | null } {
  const d = (detail ?? {}) as { checks?: CaseCheck[]; execution?: CaseExecution };
  return { checks: Array.isArray(d.checks) ? d.checks : [], execution: d.execution ?? null };
}

const TONE: Record<string, string> = {
  PASS: "text-ok",
  FAIL: "text-fail",
  UNAVAILABLE: "text-warn",
};

export function CaseChecks({ detail, compact = false }: { detail: unknown; compact?: boolean }) {
  const { checks, execution } = checksOf(detail);
  if (checks.length === 0 && !execution?.problem) return null;
  // Compact: only what explains a non-PASS — failures, unavailable required checks, execution.
  const shown = compact
    ? checks.filter((c) => c.status !== "PASS" && (c.required || c.status === "FAIL"))
    : checks;
  return (
    <div className="mt-1 space-y-0.5 font-mono text-[11px]">
      {execution?.problem && (
        <div className="text-warn">
          execution did not complete{execution.mode && execution.mode !== "unknown" ? ` (${execution.mode})` : ""}:{" "}
          {execution.problem}
        </div>
      )}
      {!compact && execution && !execution.problem && execution.mode && execution.mode !== "unknown" && (
        <div className="text-fg-faint">
          execution: {execution.mode}
          {(execution.diverged?.length ?? 0) > 0 && ` · input diverged on ${execution.diverged!.join(", ")}`}
        </div>
      )}
      {shown.map((c) => (
        <div key={c.name} className={TONE[c.status] ?? "text-fg-muted"}>
          {c.status === "PASS" ? "✓" : c.status === "FAIL" ? "✗" : "?"} {c.name}
          {!c.required && <span className="text-fg-faint"> (advisory)</span>}
          {c.status === "UNAVAILABLE" && <span> — unavailable</span>}
          {c.reason ? <span className="text-fg-muted">: {c.reason}</span> : null}
        </div>
      ))}
    </div>
  );
}
