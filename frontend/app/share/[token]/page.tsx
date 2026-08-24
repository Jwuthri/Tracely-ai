import clsx from "clsx";
import { notFound } from "next/navigation";

import { getShared, type ConvNode, type SharedGate, type SharedSession } from "@/app/lib/api";
import { convUsage, fmtUsd } from "@/app/lib/usage";
import { DOCS_URL } from "@/app/lib/site";
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
      <main
        className={clsx(
          "mx-auto w-full px-6 py-10",
          data.kind === "gate" ? "max-w-[880px]" : "max-w-[1240px]",
        )}
      >
        {data.kind === "gate" ? <GateVerdict gate={data} /> : <Conversation data={data} />}
        <Footer expiresAt={data.expires_at} />
      </main>
    </div>
  );
}

/** Same strip on both kinds of share page. Mono, uppercase, no logo, no nav. */
function Footer({ expiresAt }: { expiresAt?: number }) {
  const expires = expiresAt
    ? new Date(expiresAt * 1000).toLocaleDateString("en-US", {
        month: "short",
        day: "numeric",
        year: "numeric",
      })
    : null;
  return (
    <footer className="mt-10 border-t border-line pt-5">
      <p className="font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-faint">
        Shared via Tracely · read-only{expires ? ` · link expires ${expires}` : ""}
      </p>
      <a
        href={`${DOCS_URL}/cli#github-actions`}
        target="_blank"
        rel="noreferrer"
        className="mt-2 inline-block text-[12.5px] text-fg-muted transition-colors hover:text-signal"
      >
        Add the gate to your repo (5 lines of YAML) →
      </a>
    </footer>
  );
}

// ── CI gate verdict ──────────────────────────────────────────────────────────
// Order is the spec's, and it is load-bearing: verdict banner and case table are the whole page
// above the fold. Nothing markety renders before a visitor can answer "did this PR break it?".

function GateVerdict({ gate: g }: { gate: SharedGate }) {
  const failed = g.cases.filter((c) => c.verdict === "FAIL");
  const skipped = g.cases.filter((c) => c.verdict === "SKIP");
  const rest = g.cases.filter((c) => c.verdict !== "FAIL" && c.verdict !== "SKIP");

  return (
    <>
      <VerdictBanner gate={g} />

      <section className="reveal card mt-6 overflow-hidden">
        {g.cases.length === 0 ? (
          <div className="px-4 py-10 text-center text-[13px] text-fg-faint">
            No cases were graded in this run.
          </div>
        ) : (
          <>
            {[...failed, ...rest].map((c, i) => (
              <div
                key={i}
                className="flex flex-wrap items-center gap-3 border-b border-line/50 px-4 py-3 text-[12.5px] last:border-0"
              >
                <Badge variant={verdictVariant(c.verdict)}>{c.verdict}</Badge>
                <span className="min-w-0 flex-1 text-fg">{c.label}</span>
                {c.evaluators.map((e) => (
                  <span key={e} className="font-mono text-[11px] text-fail">
                    {e}
                  </span>
                ))}
              </div>
            ))}
            {skipped.length > 0 && (
              <details className="border-t border-line/50 px-4 py-3">
                <summary className="cursor-pointer font-mono text-[11.5px] text-fg-faint">
                  {skipped.length} not exercised
                </summary>
                <ul className="mt-2 space-y-1">
                  {skipped.map((c, i) => (
                    <li key={i} className="text-[12.5px] text-fg-muted">
                      {c.label}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </>
        )}
      </section>

      {/* The whole education, in one sentence. */}
      <p className="reveal mt-4 text-[13px] leading-relaxed text-fg-muted">
        Tracely replayed {g.total} real production failure{g.total === 1 ? "" : "s"} against this
        PR&apos;s agent. Every row is a regression test frozen from a real trace — no hand-written
        dataset.
      </p>

      <Cta signupOpen={g.signup_open} />
    </>
  );
}

function VerdictBanner({ gate: g }: { gate: SharedGate }) {
  const tone =
    g.status === "PASS"
      ? { box: "border-ok/30 bg-ok/[0.04]", text: "text-ok" }
      : g.status === "NO_COVERAGE"
        ? { box: "border-warn/30 bg-warn/[0.05]", text: "text-warn" }
        : g.status === "FAIL" || g.status === "ERROR"
          ? { box: "border-fail/30 bg-fail/[0.05]", text: "text-fail" }
          : { box: "border-info/30 bg-info/[0.04]", text: "text-info" };

  const line = [
    g.agent,
    g.pr_number ? `PR #${g.pr_number}` : null,
    g.sha,
    `${g.total} case${g.total === 1 ? "" : "s"}: ${g.passed} passed · ${g.failed} failed · ${g.skipped} skipped`,
    relativeTime(g.ran_at),
  ].filter(Boolean);

  return (
    <header className={clsx("reveal card p-6", tone.box)}>
      <div className={clsx("font-display text-[40px] font-extrabold leading-none", tone.text)}>
        {g.status}
      </div>
      <div className="mt-3 font-mono text-[12px] text-fg-muted">{line.join(" · ")}</div>
      {g.status === "NO_COVERAGE" && (
        <p className="mt-3 max-w-xl text-[12.5px] leading-snug text-warn/90">
          This run graded 0 of {g.total} case(s) — nothing was actually exercised. A gate that tests
          nothing is not a pass.
        </p>
      )}
    </header>
  );
}

/** One card, no pricing: the visitor hasn't seen value yet. `?ref=gate` is the attribution the
 *  whole feature is measured on — keep it on both branches. */
function Cta({ signupOpen }: { signupOpen: boolean }) {
  return (
    <section className="reveal card mt-8 p-6 text-center">
      <h2 className="font-display text-[20px] font-extrabold tracking-tight">
        Your agent ships to prod with no tests either.
      </h2>
      <div className="mt-4 flex flex-wrap items-center justify-center gap-3">
        {/* `/register` is this app's hosted-signup page (the spec called it `/signup`; that route
            doesn't exist). With ALLOW_PUBLIC_SIGNUP off it refuses everyone but the first user, so
            a stranger goes to the landing page instead — a second wall is the bug we're fixing.
            `?ref=gate` rides both branches: it is the attribution this whole feature is judged on. */}
        <a href={signupOpen ? "/register?ref=gate" : "/?ref=gate"} className="btn-primary">
          Gate your own agent — free
        </a>
        <a
          href={`${DOCS_URL}/product/gates`}
          target="_blank"
          rel="noreferrer"
          className="text-[13px] text-fg-muted transition-colors hover:text-signal"
        >
          How this gate works →
        </a>
      </div>
    </section>
  );
}

function relativeTime(iso: string | null): string | null {
  if (!iso) return null;
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (!Number.isFinite(mins) || mins < 0) return null;
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} day${days === 1 ? "" : "s"} ago`;
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

// ── conversation (unchanged behaviour, kept on the same route) ────────────────

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
