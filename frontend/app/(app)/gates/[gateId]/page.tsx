import clsx from "clsx";
import { getGate } from "@/app/lib/api";
import { Badge, verdictVariant } from "@/app/components/ui";
import { CopyId } from "@/app/components/CopyId";
import { GateAutoRefresh } from "@/app/components/GateAutoRefresh";
import { IconArrowLeft, IconBolt, IconCheck, IconX } from "@/app/components/icons";
import { ShareButton } from "@/app/components/ShareButton";

export default async function GatePage({ params }: { params: Promise<{ gateId: string }> }) {
  const { gateId } = await params;
  const g = await getGate(gateId);
  if (!g) {
    return (
      <div className="card p-10 text-center">
        <p className="text-fg-muted">Gate not found.</p>
        <a href="/gates" className="mt-3 inline-block text-signal">← gates</a>
      </div>
    );
  }
  const pass = g.status === "PASS";
  const nocov = g.status === "NO_COVERAGE";
  const running = g.status === "RUNNING" || (!g.finished_at && g.status !== "ERROR");
  const tone = pass
    ? { box: "border-ok/30 bg-ok/[0.04]", chip: "border-ok/40 bg-ok/10 text-ok", text: "text-ok" }
    : running
      ? { box: "border-info/30 bg-info/[0.04]", chip: "border-info/40 bg-info/10 text-info", text: "text-info" }
      : nocov
        ? { box: "border-warn/30 bg-warn/[0.05]", chip: "border-warn/40 bg-warn/10 text-warn", text: "text-warn" }
        : { box: "border-fail/30 bg-fail/[0.05]", chip: "border-fail/40 bg-fail/10 text-fail", text: "text-fail" };
  const cases = g.cases ?? [];

  return (
    <div className="space-y-6">
      <header className="reveal flex flex-wrap items-start justify-between gap-3">
        <a href="/gates" className="inline-flex items-center gap-1.5 text-[13px] text-fg-muted transition-colors hover:text-signal">
          <IconArrowLeft className="h-4 w-4" /> CI gates
        </a>
        {/* The CLI mints one of these for every PR comment; this is the same link, by hand. */}
        <ShareButton kind="gate" id={gateId} label="this gate result" />
      </header>

      {/* status banner */}
      <div
        className={clsx(
          "reveal card flex flex-wrap items-center justify-between gap-6 overflow-hidden p-5",
          tone.box,
        )}
      >
        <div className="flex items-center gap-4">
          <div className={clsx("grid h-12 w-12 place-items-center rounded-xl border", tone.chip)}>
            {pass ? <IconCheck className="h-6 w-6" /> : running ? <IconBolt className="h-6 w-6" /> : <IconX className="h-6 w-6" />}
          </div>
          <div>
            <div className={clsx("font-display text-[30px] font-extrabold leading-none", tone.text)}>
              {g.status}
            </div>
            <div className="mt-1.5 font-mono text-[12px] text-fg-muted">
              {g.agent} · {g.env}
              {g.git_ref && ` · ${g.git_ref.slice(0, 8)}`}
            </div>
            {running && (
              <div className="mt-2">
                <GateAutoRefresh />
              </div>
            )}
            {nocov && (
              <div className="mt-2 max-w-md text-[12px] leading-snug text-warn/90">
                The gate exercised nothing that produced a verdict — no CI trace matched a
                promoted case (misconfigured replay, renamed agent, input-digest drift), or every
                scenario was skipped/ungraded (no endpoint configured, no LLM key to judge). A
                gate that tests nothing is not a pass.
              </div>
            )}
          </div>
        </div>
        <div className="flex gap-3">
          <Stat n={g.passed} label="passed" tone="text-ok" />
          <Stat n={g.failed} label="failed" tone={g.failed ? "text-fail" : "text-fg"} />
          <Stat n={g.skipped} label="skipped" tone="text-fg-muted" />
        </div>
      </div>

      {(g.warnings?.length ?? 0) > 0 && (
        <div className="reveal card border-warn/30 bg-warn/[0.05] p-4" style={{ animationDelay: "40ms" }}>
          <div className="mb-2 flex items-center gap-2 text-[12.5px] font-semibold text-warn">
            ⚠️ Warnings{" "}
            <span className="font-mono text-[10px] text-fg-faint">
              (blocking when the run is FAIL/NO_COVERAGE or strict mode is on — otherwise advisory)
            </span>
          </div>
          <ul className="space-y-1">
            {g.warnings.map((w, i) => (
              <li key={i} className="font-mono text-[12px] text-fg-muted">· {w}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="reveal card grid grid-cols-2 divide-x divide-line/60 sm:grid-cols-6" style={{ animationDelay: "60ms" }}>
        <Meta k="Agent" v={g.agent ?? "—"} />
        <Meta k="Env" v={g.env} />
        <Meta k="Git ref" v={g.git_ref ? g.git_ref.slice(0, 12) : "—"} />
        <Meta k="PR" v={g.pr_number ? `#${g.pr_number}` : "—"} />
        <Meta k="Tokens" v={g.total_tokens ? g.total_tokens.toLocaleString() : "—"} />
        <Meta k="Latency" v={g.latency_ms ? `${Math.round(g.latency_ms)} ms` : "—"} />
      </div>

      <section className="reveal card overflow-hidden" style={{ animationDelay: "120ms" }}>
        <div className="border-b border-line px-4 py-3 text-[13px] font-semibold text-fg">Cases</div>
        {cases.length === 0 ? (
          <div className="px-4 py-10 text-center text-[13px] text-fg-faint">
            {running
              ? "Driving the conversations — results appear as each one is graded."
              : "No promoted cases or scenarios for this agent yet."}
          </div>
        ) : (
          cases.map((c, i) => {
            const errs = Array.isArray((c.detail as { erroring_steps?: string[] })?.erroring_steps)
              ? (c.detail as { erroring_steps: string[] }).erroring_steps
              : [];
            const reason = (c.detail as { reason?: string })?.reason;
            const quality = c.detail as { quality_pass?: boolean; quality_reason?: string };
            // An emulated conversation: `candidate_trace_id` is the thread id, and the detail
            // carries per-turn traces + how many scores actually graded it.
            const sim = c.scenario_id
              ? (c.detail as {
                  turns?: number;
                  scores?: number;
                  error?: string;
                  expectations?: number;
                  failed_expectations?: string[];
                  failed_scores?: string[];
                })
              : null;
            return (
              <div
                key={i}
                className="grid grid-cols-[92px_1fr_auto] items-center gap-3 border-b border-line/50 px-4 py-3 text-[12.5px] last:border-0"
              >
                <Badge variant={verdictVariant(c.verdict)}>{c.verdict}</Badge>
                <span className="min-w-0">
                  <span className="text-fg">{c.title}</span>
                  {sim && (
                    <span className="ml-2 font-mono text-[11px] text-fg-faint">
                      {sim.turns ?? 0} turn{sim.turns === 1 ? "" : "s"} · {sim.scores ?? 0} score
                      {sim.scores === 1 ? "" : "s"}
                      {sim.expectations
                        ? ` · ${sim.expectations} expectation${sim.expectations === 1 ? "" : "s"} checked`
                        : ""}
                    </span>
                  )}
                  {sim?.error && (
                    <span className="ml-2 font-mono text-[11px] text-fail">{sim.error}</span>
                  )}
                  {(sim?.failed_expectations ?? []).map((f, j) => (
                    <span key={j} className="mt-1 block font-mono text-[11px] text-fail">
                      ✗ {f}
                    </span>
                  ))}
                  {/* Evaluator FAILs — the cause when a conversation sank on the project's own
                      evaluators rather than an authored expectation. */}
                  {(sim?.failed_scores ?? []).map((f, j) => (
                    <span key={j} className="mt-1 block font-mono text-[11px] text-fail">
                      ✗ {f}
                    </span>
                  ))}
                  {c.verdict === "UNGRADED" && (
                    <span className="ml-2 font-mono text-[11px] text-warn">
                      ran but nothing scored it — counts against the pass rate, never as a pass
                    </span>
                  )}
                  {errs.length > 0 && <span className="ml-2 font-mono text-[11px] text-fail">errors: {errs.join(", ")}</span>}
                  {quality?.quality_pass === false && (
                    <span className="ml-2 font-mono text-[11px] text-fail">
                      answer quality{quality.quality_reason ? `: ${quality.quality_reason}` : ""}
                    </span>
                  )}
                  {c.verdict === "SKIP" && reason && <span className="ml-2 font-mono text-[11px] text-fg-faint">{reason}</span>}
                </span>
                {c.candidate_trace_id ? (
                  sim ? (
                    <a
                      href={`/sessions/${c.candidate_trace_id}`}
                      className="font-mono text-[11px] text-fg-muted transition-colors hover:text-signal"
                    >
                      view conversation →
                    </a>
                  ) : (
                    <span className="flex items-center gap-1.5 font-mono text-[11px] text-fg-faint">
                      candidate <CopyId value={c.candidate_trace_id} label="candidate trace" />
                    </span>
                  )
                ) : (
                  <span className="font-mono text-[11px] text-fg-faint">—</span>
                )}
              </div>
            );
          })
        )}
      </section>
    </div>
  );
}

function Stat({ n, label, tone }: { n: number; label: string; tone: string }) {
  return (
    <div className="rounded-lg border border-line bg-ink-900/60 px-4 py-2 text-center">
      <div className={clsx("font-display text-[22px] font-extrabold leading-none tabular-nums", tone)}>{n}</div>
      <div className="mt-1 font-mono text-[9.5px] uppercase tracking-wider text-fg-faint">{label}</div>
    </div>
  );
}

function Meta({ k, v }: { k: string; v: string }) {
  return (
    <div className="px-4 py-3">
      <div className="font-mono text-[10px] uppercase tracking-wider text-fg-faint">{k}</div>
      <div className="mt-1 font-mono text-[13px] text-fg">{v}</div>
    </div>
  );
}
