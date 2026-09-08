import { getCase, getCaseCandidates, getCaseCompare, type CaseCompare, type EvalCase, type Replay } from "@/app/lib/api";
import { describeExpectations, type Assertions } from "@/app/lib/expectations";
import { DeleteCaseButton } from "@/app/components/DeleteCaseButton";
import { CopyId } from "@/app/components/CopyId";
import { Badge, statusVariant, TypeChip, verdictVariant } from "@/app/components/ui";
import { CaseChecks, checksOf } from "@/app/components/CaseChecks";
import { ExpectationsEditor } from "@/app/components/ExpectationsEditor";
import { CandidatePicker } from "@/app/components/CandidatePicker";
import { DocLink } from "@/app/components/DocLink";
import { IconArrowLeft } from "@/app/components/icons";

/**
 * The failure-to-fix workspace (W5): one screen that answers, in order — what went wrong, what
 * the agent should have done, how to reproduce it, which candidate to check, how it differs,
 * what the check established, and what to do next. Every fact is its own line; nothing rolls
 * up into a single "protected" badge.
 */
export default async function CasePage({
  params,
  searchParams,
}: {
  params: Promise<{ caseId: string }>;
  searchParams: Promise<{ candidate?: string }>;
}) {
  const { caseId } = await params;
  const { candidate } = await searchParams;
  const c = await getCase(caseId);
  if (!c) {
    return (
      <div className="card p-10 text-center">
        <p className="text-fg-muted">Case not found.</p>
        <a href="/cases" className="mt-3 inline-block text-signal">← cases</a>
      </div>
    );
  }
  const [candidates, compare] = await Promise.all([
    getCaseCandidates(caseId),
    candidate ? getCaseCompare(caseId, candidate) : Promise.resolve(null),
  ]);
  const version = c.version ?? 1;
  const replays = c.replays ?? [];
  const validation = replays.find((r) => (r.detail as { validation?: boolean })?.validation);
  const receipt = candidate ? replays.find((r) => r.candidate_trace_id === candidate) : replays.find((r) => !(r.detail as { validation?: boolean })?.validation);
  const inSuite = c.status === "PROMOTED";
  const verified = Boolean(c.verified_candidate_trace_id) && c.verified_case_version === version;
  const command = `tracely replay ${c.agent ?? "<agent>"} --entrypoint your_module:run --case ${c.id.slice(0, 8)}`;
  const steps = c.reference_trajectory?.steps ?? [];

  return (
    <div className="space-y-6">
      <header className="reveal space-y-4">
        <a href="/cases" className="inline-flex items-center gap-1.5 text-[13px] text-fg-muted transition-colors hover:text-signal">
          <IconArrowLeft className="h-4 w-4" /> Regression cases
        </a>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="font-display text-[24px] font-extrabold tracking-tight">{c.title || "case"}</h1>
          <Badge variant={statusVariant(c.status)} dot>{c.status}</Badge>
          <span className="font-mono text-[11px] text-fg-faint">v{version}</span>
          {/* Separate facts, never one "protected" badge. */}
          {c.fail_to_pass_validated && (
            <Badge variant="warn" title="The source trace fails this case's contract — the bug is reproduced. Says nothing about a fix.">
              source failure confirmed
            </Badge>
          )}
          {verified && (
            <Badge variant="ok" title={`Candidate ${c.verified_candidate_trace_id!.slice(0, 12)}… passed the contract at v${c.verified_case_version} (${c.verified_by})`}>
              candidate verified · v{c.verified_case_version}
            </Badge>
          )}
          {c.verified_candidate_trace_id && !verified && (
            <Badge variant="neutral" title="A candidate passed an earlier version of this case; the contract changed since. Re-verify.">
              verified against v{c.verified_case_version} — re-verify
            </Badge>
          )}
          <span className="ml-auto flex items-center gap-2">
            <DocLink path="/product/cases" />
            <DeleteCaseButton caseId={c.id} title={c.title} />
          </span>
        </div>
      </header>

      {/* 1 · failure context */}
      <Section n={1} title="What went wrong">
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 text-[12.5px] sm:grid-cols-4">
          <Fact k="Agent" v={c.agent ? <a href="/gates" className="text-signal">{c.agent}</a> : "—"} />
          <Fact k="Source trace" v={<a href={`/sessions/${c.source_trace_id}`} className="font-mono text-signal">{c.source_trace_id.slice(0, 14)}…</a>} />
          <Fact k="Origin" v={c.origin} />
          <Fact k="Test exists" v={inSuite ? <span className="text-ok">yes — in the regression suite</span> : <span className="text-warn">no — draft, not gated</span>} />
        </div>
        <div className="mt-3">
          <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-fg-faint">How the original fails the contract</div>
          {validation ? (
            <>
              <Badge variant={verdictVariant(validation.verdict)}>{validation.verdict}</Badge>
              <CaseChecks detail={validation.detail} />
            </>
          ) : (
            <span className="text-[12.5px] text-fg-faint">not re-checked yet (older case)</span>
          )}
          {!inSuite && (
            <p className="mt-2 text-[12px] text-warn">
              The original run passes this contract, so the case cannot tell a fix from the bug. Tighten the expectations below
              (an argument predicate, a forbidden tool) until the original fails.
            </p>
          )}
        </div>
      </Section>

      {/* 2 · expected behaviour */}
      <Section n={2} title="What the agent should have done">
        <ul className="space-y-1 text-[13px] text-fg">
          {describeExpectations(c.assertions as Assertions).map((line, i) => (
            <li key={i}>· {line}</li>
          ))}
        </ul>
        <div className="mt-3 flex flex-wrap items-start gap-3">
          <ExpectationsEditor caseId={c.id} initial={c.assertions as Assertions} />
          <details className="text-[12px] text-fg-muted">
            <summary className="cursor-pointer">raw assertions</summary>
            <pre className="mt-1 max-w-[520px] overflow-x-auto rounded-md bg-ink-900 p-2 font-mono text-[11px]">{JSON.stringify(c.assertions, null, 2)}</pre>
          </details>
        </div>
      </Section>

      {/* 3 · execution setup */}
      <Section n={3} title="Reproduce it">
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 text-[12.5px] sm:grid-cols-4">
          <Fact k="Mode" v={<span title="Your agent code runs for real; its tool and model calls are served from the recording. A call the recording lacks fails the case rather than going live.">recorded tools + recorded model</span>} />
          <Fact k="Input" v={<span className="text-fg-muted">the original user input (in the artifact)</span>} />
          <Fact
            k="Fixtures"
            v={c.artifact_digest ? <span className="text-ok">ready · artifact v{version} · {c.artifact_digest.slice(0, 10)}</span> : <span className="text-warn">not snapshotted — depends on the source trace still existing</span>}
          />
          <Fact k="Needs" v={<span className="text-fg-muted">a Python entrypoint; no model key</span>} />
        </div>
        <div className="mt-3 flex items-center gap-2">
          <pre className="flex-1 overflow-x-auto rounded-lg border border-line bg-ink-900 px-3 py-2 font-mono text-[12px] text-fg">{command}</pre>
          <CopyId value={command} label="command" text="copy" />
        </div>
        <p className="mt-2 text-[12px] text-fg-faint">
          Recorded model outputs test your orchestration under the recorded conditions. They do not show that a changed prompt or model answers
          better — for that, run with <code className="font-mono">--live</code> and read the result as a live run. <code className="font-mono">--cmd</code> runs are always live.
        </p>
      </Section>

      {/* 4 · candidate selection */}
      <Section n={4} title="Pick the run to check">
        <CandidatePicker caseId={c.id} candidates={candidates} selected={candidate} />
      </Section>

      {/* 5 · comparison */}
      {candidate && (
        <Section n={5} title="Original vs candidate">
          {compare ? <Comparison cmp={compare} /> : <p className="text-[12.5px] text-fail">Candidate trace {candidate.slice(0, 14)}… was not found in this workspace.</p>}
        </Section>
      )}

      {/* 6 · evidence receipt */}
      {(receipt || candidate) && (
        <Section n={candidate ? 6 : 5} title="What the check established">
          {receipt ? <Receipt r={receipt} c={c} /> : <p className="text-[12.5px] text-fg-muted">Not verified yet — press Verify above to grade this candidate through the same contract CI applies.</p>}
        </Section>
      )}

      {/* 7 · next action */}
      <Section n={candidate ? 7 : receipt ? 6 : 5} title="Next">
        <ul className="space-y-1.5 text-[12.5px]">
          <li>
            {inSuite ? <span className="text-ok">✓</span> : <span className="text-warn">○</span>}{" "}
            {inSuite ? <>In the <code className="font-mono">regressions</code> suite for <b>{c.agent}</b> — every <code className="font-mono">tracely replay {c.agent}</code> runs it.</> : "Not in the suite: make the original fail the contract first (edit expectations)."}
          </li>
          <li>
            {c.last_gate ? <span className="text-ok">✓</span> : <span className="text-fg-faint">○</span>}{" "}
            {c.last_gate ? (
              <>
                Last CI result: <a href={`/gates/${c.last_gate.id}`} className="text-signal">{c.last_gate.verdict}</a> in gate {c.last_gate.status}
                {c.last_gate.run_id ? <span className="font-mono text-[11px] text-fg-faint"> · run {c.last_gate.run_id}</span> : <span className="text-warn"> · not run-scoped</span>}
              </>
            ) : (
              "No CI run has graded this case yet."
            )}
          </li>
          <li>
            <span className="text-fg-faint">○</span> Branch protection: <span className="text-fg-muted">not confirmed by Tracely</span> — make the{" "}
            <code className="font-mono">tracely/regression-gate</code> status a required check in your repository settings.{" "}
            <a href="https://docs.tracely-ai.com/product/gates" className="text-signal">how →</a>
          </li>
        </ul>
      </Section>

      {/* reference trajectory + history, secondary */}
      <details className="reveal card">
        <summary className="cursor-pointer px-4 py-3 text-[13px] font-semibold text-fg">Reference trajectory &amp; replay history</summary>
        <div className="grid grid-cols-1 gap-6 border-t border-line p-4 lg:grid-cols-2">
          <div>
            {steps.map((s, i) => (
              <div key={i} className="flex items-center gap-3 rounded-lg px-3 py-1.5" style={{ paddingLeft: 12 + (s.kind === "agent" ? 0 : 18) }}>
                <TypeChip type={s.kind === "llm" ? "GENERATION" : s.kind.toUpperCase()} />
                <span className={`font-mono text-[12.5px] ${s.level === "ERROR" ? "text-fail" : "text-fg"}`}>{s.name}</span>
                {s.level === "ERROR" && <span className="font-mono text-[11px] text-fail">✕ error</span>}
              </div>
            ))}
          </div>
          <div className="divide-y divide-line/50">
            {replays.map((r, i) => (
              <div key={i} className="grid grid-cols-[70px_1fr_auto] items-start gap-3 py-2 text-[12px]">
                <Badge variant={verdictVariant(r.verdict)}>{r.verdict}</Badge>
                <span className="min-w-0">
                  <span className="flex items-center gap-2 font-mono text-[11.5px] text-fg-muted">
                    <a href={`/cases/${c.id}?candidate=${encodeURIComponent(r.candidate_trace_id)}`} className="hover:text-signal">{r.candidate_trace_id.slice(0, 16)}…</a>
                    {(r.detail as { validation?: boolean }).validation && <span className="text-fg-faint">source check</span>}
                    {(r.detail as { case_version?: number }).case_version && <span className="text-fg-faint">v{(r.detail as { case_version?: number }).case_version}</span>}
                  </span>
                  <CaseChecks detail={r.detail} compact />
                </span>
                <span className="font-mono text-[11px] text-fg-faint">{r.created_at?.slice(0, 16).replace("T", " ")}</span>
              </div>
            ))}
            {replays.length === 0 && <span className="text-[12px] text-fg-faint">no replays yet</span>}
          </div>
        </div>
      </details>
    </div>
  );
}

function Comparison({ cmp }: { cmp: CaseCompare }) {
  const first = cmp.first_divergence;
  return (
    <div className="space-y-3">
      <div className="text-[12.5px]">
        {first === null ? (
          <span className="text-fg-muted">No relevant step differs — same tools, same arguments, same errors.</span>
        ) : (
          <span>
            First divergence at step {first + 1}: <b className="text-fg">{cmp.rows[first].divergence}</b>
          </span>
        )}
        <span className="ml-3 font-mono text-[11px] text-fg-faint">structural verdict for the candidate: {cmp.structural_verdict}</span>
      </div>
      {/* Side by side on desktop; stacked (original, then candidate) on narrow screens. */}
      <div className="hidden grid-cols-[1fr_1fr] gap-px overflow-hidden rounded-lg border border-line bg-line/40 lg:grid">
        <div className="bg-ink-900 px-3 py-2 font-mono text-[10px] uppercase tracking-wider text-fg-faint">Original</div>
        <div className="bg-ink-900 px-3 py-2 font-mono text-[10px] uppercase tracking-wider text-fg-faint">Candidate</div>
        {cmp.rows.map((row, i) => (
          <RowPair key={i} row={row} i={i} first={first} wide />
        ))}
      </div>
      <div className="space-y-2 lg:hidden">
        {cmp.rows.map((row, i) => (
          <RowPair key={i} row={row} i={i} first={first} wide={false} />
        ))}
      </div>
      {(cmp.answers.reference || cmp.answers.candidate) && (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <Answer label="Original answer" text={cmp.answers.reference} />
          <Answer label="Candidate answer" text={cmp.answers.candidate} />
        </div>
      )}
    </div>
  );
}

function RowPair({ row, i, first, wide }: { row: CaseCompare["rows"][number]; i: number; first: number | null; wide: boolean }) {
  const hot = row.divergence ? (i === first ? "bg-fail/[0.08]" : "bg-warn/[0.05]") : "";
  const cells = [row.ref, row.cand].map((s, j) => (
    <div key={j} className={`min-w-0 px-3 py-2 ${wide ? "bg-ink-800/70" : ""} ${hot}`}>
      {!wide && <div className="font-mono text-[10px] uppercase text-fg-faint">{j === 0 ? "original" : "candidate"}</div>}
      {s ? <StepCell s={s} /> : <span className="font-mono text-[11.5px] text-fg-faint">—</span>}
    </div>
  ));
  return wide ? (
    <>
      {cells[0]}
      {cells[1]}
      {row.divergence && (
        <div className="col-span-2 bg-ink-900 px-3 py-1 font-mono text-[11px] text-warn">
          step {i + 1}: {row.divergence}
        </div>
      )}
    </>
  ) : (
    <div className={`rounded-lg border border-line ${hot}`}>
      {cells[0]}
      {cells[1]}
      {row.divergence && <div className="px-3 py-1 font-mono text-[11px] text-warn">step {i + 1}: {row.divergence}</div>}
    </div>
  );
}

function StepCell({ s }: { s: NonNullable<CaseCompare["rows"][number]["ref"]> }) {
  const fmt = (v: unknown) => (v == null ? "" : typeof v === "string" ? v : JSON.stringify(v));
  return (
    <div className="font-mono text-[11.5px]">
      <div className={s.error ? "text-fail" : "text-fg"}>
        {s.name}
        {s.error && " ✕"}
      </div>
      {s.input != null && <div className="truncate text-fg-muted" title={fmt(s.input)}>args: {fmt(s.input).slice(0, 160)}</div>}
      {s.output != null && <div className="truncate text-fg-faint" title={fmt(s.output)}>→ {fmt(s.output).slice(0, 160)}</div>}
    </div>
  );
}

function Answer({ label, text }: { label: string; text: string }) {
  return (
    <div className="rounded-lg border border-line bg-ink-900 p-3">
      <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-fg-faint">{label}</div>
      <div className="whitespace-pre-wrap text-[12.5px] text-fg">{text || <span className="text-fg-faint">—</span>}</div>
    </div>
  );
}

function Receipt({ r, c }: { r: Replay; c: EvalCase }) {
  const d = r.detail as { case_version?: number; artifact_digest?: string; execution?: { mode?: string }; judges?: Record<string, { model?: string; config_digest?: string }> };
  const { checks } = checksOf(r.detail);
  const required = checks.filter((k) => k.required);
  const ran = required.filter((k) => k.status !== "UNAVAILABLE").length;
  const establishes =
    r.verdict === "PASS"
      ? `This candidate satisfies every required check (${ran}/${required.length}) under a ${d.execution?.mode ?? "recorded"} execution at case v${d.case_version ?? c.version ?? 1}. It does not prove a changed prompt answers better, and it says nothing about branch protection.`
      : r.verdict === "INCOMPLETE"
        ? `Nothing failed, but ${required.length - ran} required check(s) could not run — this candidate has not been shown to pass.`
        : "The candidate still fails the contract — the bug (or a new one) is present.";
  return (
    <div className="space-y-2 text-[12.5px]">
      <div className="flex flex-wrap items-center gap-3">
        <Badge variant={verdictVariant(r.verdict)} dot>{r.verdict}</Badge>
        <span className="font-mono text-[11.5px] text-fg-muted">candidate {r.candidate_trace_id.slice(0, 14)}…</span>
        <span className="font-mono text-[11.5px] text-fg-muted">case v{d.case_version ?? "?"}{d.artifact_digest ? ` · artifact ${d.artifact_digest.slice(0, 8)}` : ""}</span>
        <span className="font-mono text-[11.5px] text-fg-muted">mode {d.execution?.mode ?? "unknown"}</span>
        <span className="font-mono text-[11.5px] text-fg-muted">{r.created_at?.slice(0, 16).replace("T", " ")}</span>
      </div>
      <CaseChecks detail={r.detail} />
      {d.judges && Object.keys(d.judges).length > 0 && (
        <div className="font-mono text-[11px] text-fg-faint">
          judges: {Object.entries(d.judges).map(([n, j]) => `${n} (${j.model || "model?"} · ${j.config_digest ?? ""})`).join(", ")}
        </div>
      )}
      <p className="text-fg-muted">{establishes}</p>
    </div>
  );
}

function Section({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <section className="reveal card overflow-hidden" aria-labelledby={`sec-${n}`}>
      <div className="flex items-center gap-2 border-b border-line px-4 py-3">
        <span className="grid h-5 w-5 place-items-center rounded-full bg-signal/15 font-mono text-[10.5px] font-bold text-signal">{n}</span>
        <h2 id={`sec-${n}`} className="text-[13px] font-semibold text-fg">{title}</h2>
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function Fact({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div>
      <div className="font-mono text-[10px] uppercase tracking-wider text-fg-faint">{k}</div>
      <div className="mt-0.5 font-mono text-[12.5px] text-fg">{v}</div>
    </div>
  );
}
