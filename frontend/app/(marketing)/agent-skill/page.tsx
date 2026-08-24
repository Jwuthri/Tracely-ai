import type { Metadata } from "next";
import Link from "next/link";

import { DOCS_URL, GITHUB_URL, SITE_URL } from "@/app/lib/site";
import { PageShell, prose } from "../_components/PageShell";

// Target: "claude code skill" / "agent skills" / "cursor skill" + branded "tracely skill". Low
// volume today, but the term is growing and the page has a second job beyond search: it's the
// canonical install instruction the README and the docs both point at. The same command appears in
// README.md and docs/pages/skill.mdx — change all three together or one of them installs nothing.

const INSTALL = "npx skills add https://github.com/Jwuthri/Tracely-ai --skill tracely";

export const metadata: Metadata = {
  title: { absolute: "Tracely Agent Skill — Teach Claude Code and Cursor to Instrument Your Agents" },
  description:
    "Install the Tracely skill in one command and your coding agent knows how to trace an AI agent, write evaluators, and wire the CI gate — automatic instrumentation, manual spans and the traps that fail silently.",
  alternates: { canonical: "/agent-skill" },
  openGraph: {
    title: "Tracely Agent Skill — Teach Claude Code and Cursor to Instrument Your Agents",
    description:
      "One command installs the know-how: automatic and manual tracing, evaluator design, the CI gate, and the traps that silently produce a useless workspace.",
    url: `${SITE_URL}/agent-skill`,
    type: "article",
  },
};

const JSON_LD = [
  {
    "@context": "https://schema.org",
    "@type": "TechArticle",
    headline: "Tracely Agent Skill",
    description:
      "An installable skill that teaches Claude Code, Cursor and other coding agents how to instrument AI agents with Tracely, design evaluators, and gate pull requests.",
    url: `${SITE_URL}/agent-skill`,
    datePublished: "2026-08-15",
    dateModified: "2026-08-15",
    author: { "@type": "Organization", name: "Tracely", url: SITE_URL },
    publisher: { "@type": "Organization", name: "Tracely", url: SITE_URL },
  },
  {
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: [
      { "@type": "ListItem", position: 1, name: "Tracely", item: SITE_URL },
      { "@type": "ListItem", position: 2, name: "Agent skill", item: `${SITE_URL}/agent-skill` },
    ],
  },
  {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: [
      {
        "@type": "Question",
        name: "What is an agent skill?",
        acceptedAnswer: {
          "@type": "Answer",
          text: "A skill is a folder of Markdown that a coding agent loads when the task matches. It carries instructions the agent follows instead of guessing from its training data — in this case, how Tracely's SDK, evaluators and CI gate actually work, with the reference material loaded only when it is needed.",
        },
      },
      {
        "@type": "Question",
        name: "Which coding agents does the Tracely skill work with?",
        acceptedAnswer: {
          "@type": "Answer",
          text: "Any agent the open-source skills CLI supports, including Claude Code, Cursor, GitHub Copilot and Antigravity. The skill is plain Markdown with YAML frontmatter, so it can also be copied into a project by hand.",
        },
      },
      {
        "@type": "Question",
        name: "How is the skill different from the Tracely MCP server?",
        acceptedAnswer: {
          "@type": "Answer",
          text: "The MCP server gives an agent your data — traces, failure clusters, evaluators and trends from your workspace. The skill gives it the know-how: how to instrument code, what to evaluate, and how to wire the gate. They compose, and using both is the intended setup.",
        },
      },
      {
        "@type": "Question",
        name: "Does the Tracely skill cover manual instrumentation?",
        acceptedAnswer: {
          "@type": "Answer",
          text: "Yes. It defaults to automatic instrumentation, which needs one line and no span code, and carries a full reference for the manual span API — every observation type, multi-agent handoffs, RAG pipelines, shared state deltas and multimodal input — for the cases automatic tracing cannot express.",
        },
      },
    ],
  },
];

const TEACHES = [
  {
    title: "Automatic tracing",
    body: "instrument=\"auto\", the provider and framework extras, @observe, the non-patching drop-ins, LangGraph, LiteLLM, first-party agent SDKs, redaction and threads.",
  },
  {
    title: "Manual spans",
    body: "Every observation type, multi-agent handoffs, RAG pipelines, shared state deltas, multimodal I/O, and the record-replay seam that makes CI hermetic.",
  },
  {
    title: "Anything not Python",
    body: "The OTLP conventions Tracely reads, so a TypeScript, Go or Ruby service lands as a first-class trace with no Tracely code at all.",
  },
  {
    title: "Evaluator design",
    body: "Structural checks before judges, picking the level, @VARIABLE templates, advisory verdicts, sequential grading, targeting and sampling to control spend.",
  },
  {
    title: "The CI gate",
    body: "Scenarios against your endpoint, adversarial red-team runs, hermetic replay of promoted failures, and the GitHub Action that blocks the PR.",
  },
  {
    title: "Troubleshooting",
    body: "Symptom to cause to fix, ordered by how often it's the answer — including the failures that look exactly like success.",
  },
];

const TRAPS = [
  ["A missing conversation id", "turns one support thread into twelve orphan rows, and every conversation-level evaluator has nothing to grade."],
  ["A swallowed tool error", "is invisible to failure detection, clustering and the gate at once — the run looks fine and gets promoted as a good example."],
  ["No flush() before exit", "loses the last spans of every script, test and Lambda."],
  ["A dropped traceparent header", "makes the gate blind to what your agent did, so tool expectations report SKIP instead of failing."],
  ["An adversarial scenario is inverted", "— goal achieved means the attack won. Read it the usual way round and a fully successful jailbreak passes."],
];

export default function Page() {
  return (
    <PageShell>
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(JSON_LD) }} />

      {/* ---------------------------------- hero ---------------------------------- */}
      <div className="relative pb-4 pt-2">
        <div className="pointer-events-none absolute left-1/2 top-[-64px] -z-10 h-[480px] w-screen -translate-x-1/2">
          <div className="bg-blueprint absolute inset-0 opacity-60" />
          <div
            className="absolute inset-0"
            style={{ background: "radial-gradient(700px 320px at 50% 0%, rgba(34,211,238,0.16), transparent 70%)" }}
          />
          <div className="absolute inset-x-0 bottom-0 h-32 bg-gradient-to-b from-transparent to-ink-950" />
        </div>
        <div className="relative">
          <p className="font-mono text-[11px] uppercase tracking-[0.28em] text-signal/80">Agent skill</p>
          <h1 className="mt-4 font-display text-4xl font-bold leading-[1.08] tracking-tight text-fg sm:text-[54px]">
            Your coding agent already{" "}
            <span className="text-gradient-cyan">knows Tracely</span>
          </h1>
          <p className="mt-6 text-lg leading-relaxed text-fg-muted">
            One command and Claude Code, Cursor or Copilot can instrument an agent, design the
            evaluation columns, and wire the pull-request gate — without you keeping a docs tab open
            or pasting snippets it half-remembers.
          </p>
        </div>
      </div>

      {/* -------------------------------- install --------------------------------- */}
      <div className="mt-10 overflow-hidden rounded-xl border border-line bg-ink-900/60">
        <div className="border-b border-line/70 px-5 py-3 font-mono text-[11px] text-fg-faint">
          install
        </div>
        <pre className="overflow-x-auto p-5 font-mono text-[12.5px] leading-[1.75] text-signal-soft">
          {INSTALL}
        </pre>
      </div>
      <p className="mt-4 text-sm leading-relaxed text-fg-faint">
        Powered by the open-source{" "}
        <a className="text-fg-muted underline underline-offset-2 transition hover:text-fg" href="https://github.com/vercel-labs/skills" target="_blank" rel="noreferrer">
          skills
        </a>{" "}
        CLI. Add <code className="rounded bg-ink-800 px-1.5 py-0.5 font-mono text-[12.5px] text-signal-soft">-g</code>{" "}
        to install it for every project on the machine, or{" "}
        <code className="rounded bg-ink-800 px-1.5 py-0.5 font-mono text-[12.5px] text-signal-soft">--agent &apos;*&apos;</code>{" "}
        to install it into every agent you have. It&apos;s plain Markdown either way — you can also just{" "}
        <a className="text-fg-muted underline underline-offset-2 transition hover:text-fg" href={`${GITHUB_URL}/tree/master/skills/tracely`} target="_blank" rel="noreferrer">
          read it on GitHub
        </a>
        .
      </p>

      {/* -------------------------------- what for -------------------------------- */}
      <h2 className={prose.h2}>Why a skill and not just docs</h2>
      <p className={prose.p}>
        A model asked to &ldquo;add tracing to this agent&rdquo; will produce something plausible. Plausible
        is the problem: agent observability has a handful of conventions that fail <em>silently</em> when
        you get them wrong. Nothing errors, traces still arrive, the dashboard still fills up — and six
        weeks later the workspace can&apos;t answer the question it was bought for.
      </p>
      <p className={prose.p}>
        The skill front-loads exactly those conventions, then keeps the deep reference material out of
        the way until the task actually calls for it. Ask for automatic instrumentation and the manual
        span API never enters the conversation.
      </p>

      <h2 className={prose.h2}>What it teaches</h2>
      <div className="mt-6 grid gap-4 sm:grid-cols-2">
        {TEACHES.map((t) => (
          <div key={t.title} className="rounded-xl border border-line bg-ink-900/50 p-5">
            <p className="font-display font-bold text-fg">{t.title}</p>
            <p className="mt-2 text-sm leading-relaxed text-fg-muted">{t.body}</p>
          </div>
        ))}
      </div>
      <p className={prose.p}>
        It defaults to the boring answer. Automatic instrumentation is one line and no span code, so
        that&apos;s where it starts; manual spans are presented as the escape hatch they are, for the
        cases the automatic path genuinely can&apos;t express — custom retrievers, guardrails, handoff
        edges, multimodal content.
      </p>

      {/* --------------------------------- traps ---------------------------------- */}
      <h2 className={prose.h2}>The traps it stops you falling into</h2>
      <p className={prose.p}>
        Every one of these produces a green, healthy-looking workspace that is quietly worth nothing.
      </p>
      <ul className="mt-6 space-y-3">
        {TRAPS.map(([head, tail]) => (
          <li key={head} className="rounded-xl border border-line bg-ink-900/50 p-5 text-fg-muted">
            <strong className="text-fg">{head}</strong> {tail}
          </li>
        ))}
      </ul>

      {/* ---------------------------------- mcp ----------------------------------- */}
      <h2 className={prose.h2}>Pair it with the MCP server</h2>
      <p className={prose.p}>
        The skill gives your agent the know-how. The{" "}
        <a className="text-fg-muted underline underline-offset-2 transition hover:text-fg" href={`${DOCS_URL}/mcp`} target="_blank" rel="noreferrer">
          MCP server
        </a>{" "}
        gives it your data — every backend serves one at <code className="rounded bg-ink-800 px-1.5 py-0.5 font-mono text-[12.5px] text-signal-soft">/mcp</code>,
        scoped to the workspace its key belongs to.
      </p>
      <div className="mt-6 overflow-hidden rounded-xl border border-line bg-ink-900/60">
        <div className="border-b border-line/70 px-5 py-3 font-mono text-[11px] text-fg-faint">
          both, once
        </div>
        <pre className="overflow-x-auto p-5 font-mono text-[12.5px] leading-[1.75] text-signal-soft">
          {`${INSTALL}\nclaude mcp add --transport http tracely https://api.tracely-ai.com/mcp \\\n  --header "Authorization: Bearer $TRACELY_KEY"`}
        </pre>
      </div>
      <p className={prose.p}>
        With both connected, the useful ask stops being a code request and becomes a product one:{" "}
        <em>&ldquo;look at the last 20 traces, work out what&apos;s failing, and add an evaluation column
        that catches it.&rdquo;</em>
      </p>

      {/* --------------------------------- next ----------------------------------- */}
      <h2 className={prose.h2}>Next</h2>
      <ul className={prose.ul}>
        <li>
          <a className="text-fg underline underline-offset-2" href={DOCS_URL} target="_blank" rel="noreferrer">
            SDK documentation
          </a>{" "}
          — the source the skill distils, with runnable examples per provider and framework.
        </li>
        <li>
          <Link className="text-fg underline underline-offset-2" href="/llm-evaluation">
            LLM evaluation
          </Link>{" "}
          — the concepts behind the evaluator columns the skill helps you design.
        </li>
        <li>
          <a className="text-fg underline underline-offset-2" href={GITHUB_URL} target="_blank" rel="noreferrer">
            Tracely on GitHub
          </a>{" "}
          — MIT, self-hostable, and where the skill lives.
        </li>
      </ul>
    </PageShell>
  );
}
