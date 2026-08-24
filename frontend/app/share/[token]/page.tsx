import clsx from "clsx";
import { notFound } from "next/navigation";

import { getShared, type ConvNode, type SharedGate, type SharedSession } from "@/app/lib/api";
import { convUsage, fmtUsd } from "@/app/lib/usage";
import { Badge, verdictVariant } from "@/app/components/ui";
import { SessionView } from "@/app/components/SessionView";

// Deliberately OUTSIDE the (app) route group: that layout calls requireSession() and renders the
// sidebar/topbar. A share link has no session and no navigation — it is one read-only page.
// `/share/` is also listed in middleware.ts's PUBLIC and isPublicClerk matchers.

export const metadata = {
  title: "Shared via Tracely",
  robots: { index: false, follow: false }, // unlisted, not secret — keep links out of search results
};

export default async function SharedPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  const data = await getShared(token);
  if (!data) notFound(); // expired, revoked, forged, or deleted — all indistinguishable, by design

  return (
    <div className="bg-grid min-h-screen">
      <main className="mx-auto w-full max-w-[1240px] px-8 py-8">
        {data.kind === "gate" ? <GateVerdict gate={data} /> : <Conversation data={data} />}
        <Cta kind={data.kind === "gate" ? "gate" : "conversation"} />
      </main>
    </div>
  );
}

/** The logged-out CTA. This page exists because the link lands in a public pull request, so the
 *  one job below the fold is telling a stranger what they just looked at. */
// ponytail: plain copy + one link. Oscar's spec (conv-t6) drops in here without touching anything
// above it.
function Cta({ kind }: { kind: "gate" | "conversation" }) {
  return (
    <footer className="reveal mt-10 border-t border-line pt-6 text-center">
      <p className="text-[13px] text-fg-muted">
        {kind === "gate" ? (
          <>
            This is a <span className="text-fg">Tracely</span> regression gate — CI tests generated
            from real production failures, run on every pull request.
          </>
        ) : (
          <>
            Traced and graded by <span className="text-fg">Tracely</span> — every agent run scored,
            every failure turned into a test that gates the next pull request.
          </>
        )}
      </p>
      <a href="/" className="mt-3 inline-block btn-primary">
        Try Tracely free →
      </a>
    </footer>
  );
}

function Conversation({ data }: { data: SharedSession }) {
  const { thread_id: threadId, turns, scores } = data;
  const conv: ConvNode = {
    thread: threadId,
    turns: turns.length,
    first_input: turns[0]?.input ?? null,
    last_output: turns[turns.length - 1]?.output ?? null,
    tokens: turns.reduce((a, t) => a + (t.tokens || 0), 0),
    cost: turns.reduce((a, t) => a + (t.cost || 0), 0),
    first_ts: turns[0]?.ts ?? "",
    last_ts: turns[turns.length - 1]?.ts ?? "",
    last_trace_id: turns[turns.length - 1]?.trace_id ?? threadId,
    failing: turns.some((t) => t.failing === 1 || t.verdict === "FAIL") ? 1 : 0,
    turnsData: turns,
    scores: scores ?? [],
  };
  const usage = convUsage(conv);

  return (
    <>
      <header className="reveal">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <h1 className="font-display text-[22px] font-extrabold tracking-tight">Conversation</h1>
          <span className="font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-faint">
            Shared via Tracely · read-only
          </span>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-3 font-mono text-[11.5px] text-fg-faint">
          <span>{turns.length} turns</span>
          {usage.total_tokens ? <span>{usage.total_tokens.toLocaleString("en-US")} tokens</span> : null}
          {usage.cost ? <span className="text-warn/90">{fmtUsd(usage.cost)}</span> : null}
        </div>
      </header>

      <div className="mt-6">
        <SessionView conv={conv} turns={turns} shared />
      </div>
    </>
  );
}

/** The gate detail page, minus everything a stranger may not have: no trace links, no candidate
 *  ids, no navigation back into the app. Same tone vocabulary as `(app)/gates/[gateId]`. */
function GateVerdict({ gate: g }: { gate: SharedGate }) {
  const pass = g.status === "PASS";
  const nocov = g.status === "NO_COVERAGE";
  const running = g.status === "RUNNING" || (!g.finished_at && g.status !== "ERROR");
  const tone = pass
    ? { box: "border-ok/30 bg-ok/[0.04]", text: "text-ok" }
    : running
      ? { box: "border-info/30 bg-info/[0.04]", text: "text-info" }
      : nocov
        ? { box: "border-warn/30 bg-warn/[0.05]", text: "text-warn" }
        : { box: "border-fail/30 bg-fail/[0.05]", text: "text-fail" };

  return (
    <>
      <header className="reveal flex flex-wrap items-baseline justify-between gap-3">
        <h1 className="font-display text-[22px] font-extrabold tracking-tight">
          Regression gate{g.agent ? ` · ${g.agent}` : ""}
        </h1>
        <span className="font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-faint">
          Shared via Tracely · read-only
        </span>
      </header>

      <div
        className={clsx(
          "reveal card mt-6 flex flex-wrap items-center justify-between gap-6 p-5",
          tone.box,
        )}
      >
        <div>
          <div className={clsx("font-display text-[30px] font-extrabold leading-none", tone.text)}>
            {g.status}
          </div>
          <div className="mt-1.5 font-mono text-[12px] text-fg-muted">
            {g.env}
            {g.git_ref && ` · ${g.git_ref.slice(0, 8)}`}
            {g.pr_number ? ` · PR #${g.pr_number}` : ""}
          </div>
        </div>
        <div className="flex gap-3">
          <Stat n={g.passed} label="passed" tone="text-ok" />
          <Stat n={g.failed} label="failed" tone={g.failed ? "text-fail" : "text-fg"} />
          <Stat n={g.skipped} label="skipped" tone="text-fg-muted" />
        </div>
      </div>

      {g.status === "FAIL" && (
        <p className="reveal mt-4 text-[13px] leading-relaxed text-fg-muted">
          These regression tests were promoted from <span className="text-fg">real production
          failures</span>. A FAIL means this change reintroduces — or fails to fix — a known one.
        </p>
      )}
      {nocov && (
        <p className="reveal mt-4 text-[13px] leading-relaxed text-warn/90">
          No coverage: this run graded 0 of {g.total} case(s). A gate that tests nothing is not a
          pass.
        </p>
      )}

      {(g.warnings?.length ?? 0) > 0 && (
        <ul className="reveal card mt-4 space-y-1 border-warn/30 bg-warn/[0.05] p-4">
          {g.warnings.map((w, i) => (
            <li key={i} className="font-mono text-[12px] text-fg-muted">
              ⚠️ {w}
            </li>
          ))}
        </ul>
      )}

      <section className="reveal card mt-6 overflow-hidden">
        <div className="border-b border-line px-4 py-3 text-[13px] font-semibold text-fg">Cases</div>
        {g.cases.length === 0 ? (
          <div className="px-4 py-10 text-center text-[13px] text-fg-faint">
            {running ? "Still running — results appear as each case is graded." : "No cases."}
          </div>
        ) : (
          g.cases.map((c, i) => (
            <div
              key={i}
              className="grid grid-cols-[92px_1fr] items-start gap-3 border-b border-line/50 px-4 py-3 text-[12.5px] last:border-0"
            >
              <Badge variant={verdictVariant(c.verdict)}>{c.verdict}</Badge>
              <span className="min-w-0">
                <span className="text-fg">{c.title}</span>
                {caseReasons(c.detail).map((r, j) => (
                  <span key={j} className="mt-1 block font-mono text-[11px] text-fail">
                    ✗ {r}
                  </span>
                ))}
              </span>
            </div>
          ))
        )}
      </section>
    </>
  );
}

/** Why a case is not a PASS, in plain lines. Mirrors `case_reason()` in the SDK's PR comment —
 *  the reader of this page and the reader of that comment should see the same sentence. */
function caseReasons(detail: Record<string, unknown>): string[] {
  const d = detail ?? {};
  const list = (k: string) => (Array.isArray(d[k]) ? (d[k] as string[]) : []);
  const out: string[] = [];
  if (typeof d.error === "string" && d.error) out.push(d.error);
  out.push(...list("failed_expectations"), ...list("failed_scores"));
  if (list("missing_tools").length) out.push(`missing tools: ${list("missing_tools").join(", ")}`);
  if (list("run_errors").length) out.push(`run failed: ${list("run_errors").join(", ")}`);
  else if (list("erroring_steps").length) out.push(`errors: ${list("erroring_steps").join(", ")}`);
  if (d.tools_ok === false && !list("missing_tools").length) out.push("tool sequence mismatch");
  if (d.quality_pass === false) {
    out.push(`answer quality below bar${d.quality_reason ? `: ${d.quality_reason}` : ""}`);
  }
  if (typeof d.reason === "string" && d.reason) out.push(d.reason);
  return out;
}

function Stat({ n, label, tone }: { n: number; label: string; tone: string }) {
  return (
    <div className="rounded-lg border border-line bg-ink-900/60 px-4 py-2 text-center">
      <div className={clsx("font-display text-[22px] font-extrabold leading-none tabular-nums", tone)}>
        {n}
      </div>
      <div className="mt-1 font-mono text-[9.5px] uppercase tracking-wider text-fg-faint">{label}</div>
    </div>
  );
}
