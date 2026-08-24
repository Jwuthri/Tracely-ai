"use client";

import { useRef, useState, type ReactNode, type SVGProps } from "react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { FleetPeek } from "./_components/FleetPeek";
import { ScramblePipeline } from "./_components/ScramblePipeline";
import { PipelinePeek } from "@/app/components/PipelinePeek";

gsap.registerPlugin(useGSAP, ScrollTrigger);

const GITHUB = "https://github.com/Jwuthri/Tracely-ai";
const APP = "/dashboard"; // same app — the authed shell lives in the (app) route group
const DOCS = "https://doc.tracely-ai.com";
const LINKEDIN = "https://www.linkedin.com/in/julien-wuthrich-a75156119/";
const INSTALL = 'pip install "tracely-ai[openai]"';
const API = "https://api.tracely-ai.com"; // the hosted backend — self-hosters swap in their own

/* ---------------------------------- ui bits ---------------------------------- */

/* A flat pill of solid colour reads as 2021. The lift comes from three cheap things: a squarer
   radius, a top-edge inner highlight so the surface catches light, and a hairline ring. */
const btnPrimary =
  "inline-flex items-center gap-2 whitespace-nowrap rounded-xl bg-gradient-to-b from-signal-soft to-signal px-5 py-2.5 text-sm font-semibold text-ink-950 ring-1 ring-inset ring-white/25 shadow-[0_1px_0_rgba(255,255,255,0.35)_inset,0_2px_8px_-3px_rgb(var(--c-signal)/0.4)] transition duration-300 hover:brightness-[1.07] hover:shadow-[0_1px_0_rgba(255,255,255,0.4)_inset,0_4px_14px_-4px_rgb(var(--c-signal)/0.55)]";
const btnGhost =
  "inline-flex items-center gap-2 whitespace-nowrap rounded-xl border border-line-bright/60 bg-ink-800/70 px-5 py-2.5 text-sm font-medium text-fg-muted shadow-[0_1px_0_rgba(255,255,255,0.05)_inset] backdrop-blur-sm transition duration-300 hover:border-line-bright hover:bg-ink-800 hover:text-fg";

/** The install line, click to copy. Two call sites (hero, final CTA) — hence a component. */
function CopyCmd({ cmd }: { cmd: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        // ponytail: clipboard is unavailable on insecure origins — the text stays selectable either way.
        try {
          await navigator.clipboard.writeText(cmd);
          setCopied(true);
          setTimeout(() => setCopied(false), 1600);
        } catch {
          /* no clipboard — nothing to do */
        }
      }}
      className="inline-flex items-center gap-3 whitespace-nowrap rounded-xl border border-line bg-ink-900/70 px-4 py-2 font-mono text-[11px] text-fg-muted transition duration-300 hover:border-line-bright hover:text-fg sm:text-[12.5px]"
      aria-label={`Copy: ${cmd}`}
    >
      <span className="text-fg-faint">$</span>
      <span>{cmd}</span>
      <span className={`text-[10px] uppercase tracking-[0.16em] ${copied ? "text-ok" : "text-fg-faint"}`}>
        {copied ? "copied" : "copy"}
      </span>
    </button>
  );
}

/* Plans. Deliberately three: self-host is the honest default (it's MIT and complete), Free is
   the hosted on-ramp, Team is the paid tier. Prices live here rather than in a CMS because
   there's exactly one page that shows them. */
const PLANS: {
  name: string;
  blurb: string;
  price: string;
  per?: string;
  features: string[];
  cta: string;
  href: string;
  featured?: boolean;
}[] = [
  {
    name: "Self-host",
    blurb: "The entire product, MIT-licensed, on your own infrastructure.",
    price: "$0",
    per: "forever",
    features: [
      "Every feature — no paywalled internals",
      "Your traces never leave your network",
      "One-click deploy to Railway, or docker compose",
      "Unlimited traces, agents and seats",
      "Community support on GitHub",
    ],
    cta: "Deploy your own",
    href: GITHUB,
  },
  {
    name: "Free",
    blurb: "Hosted, for trying it on a real agent without running ClickHouse.",
    price: "$0",
    per: "/month",
    features: [
      "20k traces / month",
      "7-day trace retention",
      "All evaluators + failure clustering",
      "CI gate on one agent",
      "3 workspaces, 3 seats",
    ],
    cta: "Start free",
    href: APP,
    featured: true,
  },
  {
    name: "Team",
    blurb: "For teams gating real releases on real production failures.",
    price: "$49",
    per: "/month",
    features: [
      "1M traces / month",
      "90-day retention",
      "Unlimited agents + CI gates",
      "10 workspaces and seats, one subscription",
      "Judge calibration + adversarial scenarios",
      "Email support",
    ],
    cta: "Start free, upgrade later",
    href: APP,
  },
];

type P = SVGProps<SVGSVGElement>;
const base = (p: P) => ({
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.7,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  ...p,
});

const IconArrow = (p: P) => (
  <svg {...base(p)}>
    <path d="M5 12h14M13 6l6 6-6 6" />
  </svg>
);
const IconGitHub = (p: P) => (
  <svg viewBox="0 0 16 16" fill="currentColor" {...p}>
    <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
  </svg>
);
const IconColumns = (p: P) => (
  <svg {...base(p)}>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M9 4v16M15 4v16M3 9h18" />
  </svg>
);
const IconCluster = (p: P) => (
  <svg {...base(p)}>
    <circle cx="7" cy="7" r="2.4" />
    <circle cx="17" cy="6" r="1.8" />
    <circle cx="16.5" cy="16.5" r="2.6" />
    <circle cx="6.5" cy="17" r="1.8" />
    <path d="M9 8.5l5.5 6M15.3 7.2l-6.9 8.3" opacity=".45" />
  </svg>
);
const IconSnowflake = (p: P) => (
  <svg {...base(p)}>
    <path d="M12 2v20M4 6l16 12M20 6L4 18M9 3.5 12 6l3-2.5M9 20.5 12 18l3 2.5M3.5 8.5 6 11l-2.5 2M20.5 8.5 18 11l2.5 2" />
  </svg>
);
const IconShield = (p: P) => (
  <svg {...base(p)}>
    <path d="M12 3 5 6v5c0 4.2 2.9 7.6 7 9 4.1-1.4 7-4.8 7-9V6l-7-3Z" />
    <path d="m9 12 2 2 4-4" />
  </svg>
);
const IconTrend = (p: P) => (
  <svg {...base(p)}>
    <path d="M3 3v18h18" />
    <path d="M7 15v3M12 10v8M17 6v12" />
  </svg>
);
const IconScale = (p: P) => (
  <svg {...base(p)}>
    <path d="M12 3v18M7 21h10M5 7h14" />
    <path d="M5 7 2.5 12.5a2.5 2.5 0 0 0 5 0L5 7ZM19 7l-2.5 5.5a2.5 2.5 0 0 0 5 0L19 7Z" />
  </svg>
);
const IconX = (p: P) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="9" />
    <path d="m9 9 6 6M15 9l-6 6" />
  </svg>
);
const IconCheck = (p: P) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="9" />
    <path d="m8.5 12.5 2.5 2.5 4.5-5" />
  </svg>
);
const IconSpinner = (p: P) => (
  <svg {...base(p)}>
    <path d="M12 3a9 9 0 1 0 9 9" />
  </svg>
);

/** The Tracely mark — same geometry as the app shell's (components/Sidebar.tsx, (auth)/_ui.tsx). */
function Mark({ size = 34 }: { size?: number }) {
  return (
    <span
      className="relative grid shrink-0 place-items-center rounded-[11px] border border-signal/30 bg-signal/10 shadow-[0_0_22px_-6px_rgba(34,211,238,0.7)]"
      style={{ width: size, height: size, borderRadius: size * 0.32 }}
    >
      <svg width={size * 0.5} height={size * 0.5} viewBox="0 0 24 24" fill="none" aria-hidden>
        <path d="M12 2 22 12 12 22 2 12Z" stroke="#22d3ee" strokeWidth="1.8" strokeLinejoin="round" />
        <circle cx="12" cy="12" r="2.7" fill="#22d3ee" />
      </svg>
    </span>
  );
}

/** Words wrapped in overflow-hidden masks for the hero line-reveal. */
function MaskWords({ text, wordClass = "" }: { text: string; wordClass?: string }) {
  return (
    <>
      {text.split(" ").map((w, i) => (
        <span key={i}>
          {i > 0 && " "}
          <span className="inline-block overflow-hidden pb-[0.12em] -mb-[0.12em] align-bottom">
            <span className={`hero-word inline-block will-change-transform ${wordClass}`}>{w}</span>
          </span>
        </span>
      ))}
    </>
  );
}

function StateWords({ text }: { text: string }) {
  return (
    <>
      {text.split(" ").map((w, i) => (
        <span key={i}>
          {i > 0 && " "}
          <span className="state-word inline-block">{w}</span>
        </span>
      ))}
    </>
  );
}

function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <div className="sec-reveal mx-auto flex max-w-md items-center gap-4">
      <div className="hairline-x flex-1" />
      <span className="eyebrow whitespace-nowrap">{children}</span>
      <div className="hairline-x flex-1" />
    </div>
  );
}

/* --------------------------------- hero data --------------------------------- */

const ROWS = [
  { type: "agent", chip: "text-t_agent", bar: "bg-t_agent/80", name: "support_agent", left: 0, w: 100, dur: "8.4s" },
  { type: "llm", chip: "text-t_llm", bar: "bg-t_llm/80", name: "plan · claude-sonnet-5", left: 4, w: 16, dur: "1.2s" },
  { type: "tool", chip: "text-t_tool", bar: "bg-t_tool/80", name: "search_orders", left: 22, w: 12, dur: "0.9s" },
  { type: "tool", chip: "text-t_tool", bar: "bg-t_tool/80", name: "refund_api", left: 36, w: 34, dur: "3.1s", err: true },
  { type: "llm", chip: "text-t_llm", bar: "bg-t_llm/80", name: "respond · claude-sonnet-5", left: 74, w: 22, dur: "1.4s" },
];

const MARQUEE = [
  "OpenAI",
  "Anthropic",
  "Google Gemini",
  "LangChain",
  "LangGraph",
  "LiteLLM",
  "CrewAI",
  "Mistral",
  "any OTLP source",
];

const STEPS = [
  {
    n: "01",
    title: "Trace",
    body: "Every production run streams in over OTLP — durable blob first, one indexed row per span. Agent, conversation and tool semantics are first-class columns, not strings.",
  },
  {
    n: "02",
    title: "Detect",
    body: "Online evaluators grade each trace as it lands — LLM-as-judge at conversation, run or span level. One FAIL on a blocking evaluator flips the verdict.",
  },
  {
    n: "03",
    title: "Freeze",
    body: "One click promotes a failing trace into a hermetic case: recorded input, tool and LLM fixtures bundled, a fail-to-pass contract attached.",
  },
  {
    n: "04",
    title: "Gate",
    body: "tracely replay re-runs the suite on every PR — offline, deterministic, $0 in model spend — and exits non-zero on the merge that regresses it.",
  },
];

const FEATURES = [
  {
    icon: IconColumns,
    title: "Evaluators as columns",
    body: "Judges live where you look: every evaluator is a column on the trace table, and scores stream into the grid live over SSE as they run.",
  },
  {
    icon: IconCluster,
    title: "Failure clustering",
    body: "Structural + semantic clustering groups failing traces into named issues — with a description, a proposed fix and a suggested evaluator to catch it next time.",
  },
  {
    icon: IconSnowflake,
    title: "Hermetic replay",
    body: "Cases replay against recorded tool and LLM fixtures — no API keys, no model spend, no flakes. Add --live when you want real calls.",
  },
  {
    icon: IconShield,
    title: "The PR gate",
    body: "tracely gate exits non-zero, posts the commit status and upserts a PR comment. Two lines of YAML in the workflow you already have.",
  },
  {
    icon: IconTrend,
    title: "Trends & meta-analysis",
    body: "Daily failure and gate pass-rates, plus per-agent cross-metric analysis: Spearman correlations, z-score outliers, LLM-synthesized findings.",
  },
  {
    icon: IconScale,
    title: "Judge calibration",
    body: "Label judge verdicts against human review, get per-evaluator agreement, and catch an over-flagging judge before you let it gate a release.",
  },
];

/* --------------------------------- code lines -------------------------------- */

const c = {
  cm: "text-fg-faint",
  k: "text-info",
  s: "text-signal-soft/90",
  kw: "text-t_retriever",
  fn: "text-info",
  p: "text-fg-muted",
};

const YAML_LINES: ReactNode[] = [
  <span key="0" className={c.cm}># .github/workflows/tracely.yml</span>,
  <span key="1"><span className={c.k}>on</span><span className={c.p}>: pull_request</span></span>,
  <span key="2"><span className={c.k}>jobs</span><span className={c.p}>:</span></span>,
  <span key="3" className={c.p}>{"  "}<span className={c.k}>gate</span>:</span>,
  <span key="4" className={c.p}>{"    "}<span className={c.k}>runs-on</span>: ubuntu-latest</span>,
  <span key="5" className={c.p}>{"    "}<span className={c.k}>steps</span>:</span>,
  <span key="6" className={c.p}>{"      "}- <span className={c.k}>uses</span>: actions/checkout@v4</span>,
  <span key="7" className={c.cm}>{"      "}# → run your agent here; traces stream in with env=ci</span>,
  <span key="8" className={c.p}>{"      "}- <span className={c.k}>uses</span>: Jwuthri/Tracely-ai/.github/actions/tracely-gate@master</span>,
  <span key="9" className={c.p}>{"        "}<span className={c.k}>with</span>:</span>,
  <span key="10" className={c.p}>{"          "}<span className={c.k}>agent</span>: support-agent</span>,
  <span key="11" className={c.p}>{"          "}<span className={c.k}>api</span>:{"   "}<span className={c.s}>https://tracely.your-co.dev</span></span>,
  <span key="12" className={c.p}>{"          "}<span className={c.k}>key</span>:{"   "}<span className={c.s}>{"${{ secrets.TRACELY_KEY }}"}</span></span>,
];

const PY_LINES: ReactNode[] = [
  <span key="0"><span className={c.kw}>import</span><span className={c.p}> tracely_sdk </span><span className={c.kw}>as</span><span className={c.p}> tracely</span></span>,
  <span key="1"><span className={c.kw}>from</span><span className={c.p}> openai </span><span className={c.kw}>import</span><span className={c.p}> OpenAI</span></span>,
  <span key="2">{" "}</span>,
  <span key="3"><span className={c.p}>tracely.</span><span className={c.fn}>init</span><span className={c.p}>(endpoint=</span><span className={c.s}>&quot;https://tracely.your-co.dev&quot;</span><span className={c.p}>,</span></span>,
  <span key="4" className={c.p}>{"             "}api_key=os.environ[<span className={c.s}>&quot;TRACELY_KEY&quot;</span>],</span>,
  <span key="5" className={c.p}>{"             "}service_name=<span className={c.s}>&quot;support-agent&quot;</span>, env=<span className={c.s}>&quot;prod&quot;</span>)</span>,
  <span key="6">{" "}</span>,
  <span key="7"><span className={c.kw}>with</span><span className={c.p}> tracely.</span><span className={c.fn}>trace</span><span className={c.p}>(agent=</span><span className={c.s}>&quot;support-agent&quot;</span><span className={c.p}>, conversation=</span><span className={c.s}>&quot;conv-42&quot;</span><span className={c.p}>):</span></span>,
  <span key="8" className={c.p}>{"    "}OpenAI().chat.completions.create(...)  <span className={c.cm}># traced — zero span code</span></span>,
];

const MCP_LINES: ReactNode[] = [
  <span key="0" className={c.cm}># one line — the endpoint ships with the API</span>,
  <span key="1"><span className={c.fn}>claude</span><span className={c.p}> mcp add --transport http tracely {"\\"}</span></span>,
  <span key="2" className={c.p}>{"  "}<span className={c.s}>{API}/mcp</span> {"\\"}</span>,
  <span key="3" className={c.p}>{"  "}--header <span className={c.s}>&quot;Authorization: Bearer $TRACELY_KEY&quot;</span></span>,
  <span key="4">{" "}</span>,
  <span key="5" className={c.cm}>› what failed in the last 20 traces, and</span>,
  <span key="6" className={c.cm}>{"  "}add a column that catches it next time</span>,
  <span key="7">{" "}</span>,
  <span key="8"><span className={c.kw}>✓</span><span className={c.p}> get_trace, list_clusters, create_evaluator …</span></span>,
];

// The other half of the MCP story: MCP hands the agent your data, the skill hands it the know-how.
// Command must stay identical to README.md, docs/pages/skill.mdx and /agent-skill.
const SKILL_LINES: ReactNode[] = [
  <span key="0" className={c.cm}># plain Markdown — Claude Code, Cursor, Copilot …</span>,
  <span key="1"><span className={c.fn}>npx</span><span className={c.p}> skills add </span><span className={c.s}>{GITHUB}</span> <span className={c.p}>{"\\"}</span></span>,
  <span key="2" className={c.p}>{"  "}--skill <span className={c.s}>tracely</span></span>,
  <span key="3">{" "}</span>,
  <span key="4"><span className={c.kw}>✓</span><span className={c.p}> auto + manual tracing · evaluators · CI gate</span></span>,
];

/* ---------------------------------- page ---------------------------------- */

export default function Landing() {
  const rootRef = useRef<HTMLDivElement>(null);

  useGSAP(
    () => {
      const root = rootRef.current;
      if (!root) return;
      const q = gsap.utils.selector(root);

      // nav gets its frosted plate as soon as the page moves
      ScrollTrigger.create({ start: 40, toggleClass: { targets: q(".site-nav"), className: "nav-blur" } });

      const mm = gsap.matchMedia(root);

      mm.add("(prefers-reduced-motion: no-preference)", () => {
        /* ---------- initial states (before first paint) ---------- */
        gsap.set(q(".site-nav"), { autoAlpha: 0, y: -16 });
        // LCP: the hero subhead inside .hero-stagger is Lighthouse's LCP element. Animating its
        // OPACITY meant the browser did not count it as painted until GSAP had loaded, hydrated
        // and run — 3.2s of pure "render delay" on mobile. Transform only: it paints with the
        // first frame and still slides into place. Never put opacity on the LCP element.
        gsap.set(q(".hero-stagger"), { y: 26 });
        gsap.set(q(".hero-word"), { yPercent: 115 });
        gsap.set(q(".hero-frame"), { autoAlpha: 0, y: 64, scale: 0.965 });
        gsap.set(q(".float-card"), { autoAlpha: 0, y: 34 });
        gsap.set(q(".tr-row"), { autoAlpha: 0, x: -14 });
        gsap.set(q(".tr-bar"), { scaleX: 0, transformOrigin: "left center" });
        gsap.set(q(".tr-err"), { autoAlpha: 0 });
        gsap.set(q(".tr-pill"), { autoAlpha: 0, scale: 0.5 });
        gsap.set(q(".tr-toast"), { autoAlpha: 0, y: 16 });
        gsap.set(q(".tr-stamp"), { autoAlpha: 0, scale: 1.7, rotate: -10 });

        /* ---------- the hero loop: trace → fail → cluster → freeze → gate ---------- */
        const loop = gsap.timeline({ paused: true, repeat: -1, repeatDelay: 1.4 });
        loop
          .to(q(".tr-row"), { autoAlpha: 1, x: 0, duration: 0.4, stagger: 0.18, ease: "power2.out" })
          .to(q(".tr-bar"), { scaleX: 1, duration: 0.55, stagger: 0.18, ease: "power2.inOut" }, "<+=0.2")
          .to(q(".tr-err"), { autoAlpha: 1, duration: 0.25 }, "-=0.35")
          .to(q(".tr-pill"), { autoAlpha: 1, scale: 1, duration: 0.35, stagger: 0.22, ease: "back.out(2.2)" }, "+=0.25")
          .to(q(".tr-pill-fail"), { opacity: 0.4, duration: 0.16, repeat: 3, yoyo: true }, "+=0.1")
          .to(q(".tr-toast-cluster"), { autoAlpha: 1, y: 0, duration: 0.45, ease: "power3.out" }, "+=0.35")
          .to(q(".tr-toast-case"), { autoAlpha: 1, y: 0, duration: 0.45, ease: "power3.out" }, "+=1.0")
          .to(q(".tr-stamp"), { autoAlpha: 1, scale: 1, rotate: -4, duration: 0.3, ease: "power4.in" }, "+=1.0")
          .to(q(".tr-frame"), { x: 4, duration: 0.05, repeat: 5, yoyo: true, ease: "none" }, "<+=0.25")
          .set(q(".tr-frame"), { x: 0 })
          .to({}, { duration: 2.0 })
          .to(q(".tr-row, .tr-err, .tr-pill, .tr-toast, .tr-stamp"), { autoAlpha: 0, duration: 0.5 })
          .to(q(".tr-bar"), { scaleX: 0, duration: 0.4 }, "<")
          .set(q(".tr-row"), { x: -14 })
          .set(q(".tr-pill"), { scale: 0.5 })
          .set(q(".tr-toast"), { y: 16 })
          .set(q(".tr-stamp"), { scale: 1.7, rotate: -10 });

        /* ---------- intro ---------- */
        gsap
          .timeline({ defaults: { ease: "power3.out" }, delay: 0.15 })
          .to(q(".site-nav"), { autoAlpha: 1, y: 0, duration: 0.6 })
          .to(q(".hero-word"), { yPercent: 0, duration: 0.9, stagger: 0.06, ease: "power4.out" }, 0.15)
          .to(q(".hero-stagger"), { y: 0, duration: 0.7, stagger: 0.1 }, 0.45)
          .to(q(".hero-frame"), { autoAlpha: 1, y: 0, scale: 1, duration: 1.0 }, 0.75)
          .to(q(".float-card"), { autoAlpha: 1, y: 0, duration: 0.7, stagger: 0.15 }, 1.1)
          .add(() => loop.play(), 1.5);

        /* ---------- scroll: section headers ---------- */
        gsap.utils.toArray<Element>(q(".sec-reveal")).forEach((el) => {
          gsap.from(el, {
            autoAlpha: 0,
            y: 28,
            duration: 0.7,
            ease: "power3.out",
            scrollTrigger: { trigger: el, start: "top 86%", once: true },
          });
        });

        /* ---------- scroll: the loop line draws, a run travels it ---------- */
        const path = q(".loop-path")[0] as unknown as SVGPathElement | undefined;
        if (path) {
          const len = path.getTotalLength();
          gsap.set(path, { strokeDasharray: len, strokeDashoffset: len });
          gsap.to(path, {
            strokeDashoffset: 0,
            ease: "none",
            scrollTrigger: { trigger: q(".loop-steps")[0], start: "top 75%", end: "bottom 60%", scrub: 1 },
          });
        }
        const track = q(".loop-track")[0] as HTMLElement | undefined;
        const dot = q(".loop-dot")[0];
        if (track && dot) {
          gsap.to(dot, {
            x: () => track.offsetWidth - 10,
            ease: "none",
            scrollTrigger: { trigger: q(".loop-steps")[0], start: "top 75%", end: "bottom 60%", scrub: 1, invalidateOnRefresh: true },
          });
        }
        gsap.from(q(".step-card"), {
          autoAlpha: 0,
          y: 34,
          duration: 0.6,
          stagger: 0.12,
          ease: "power3.out",
          scrollTrigger: { trigger: q(".loop-steps")[0], start: "top 80%", once: true },
        });

        /* ---------- scroll: statement lights up word by word ---------- */
        gsap.fromTo(
          q(".state-word"),
          { opacity: 0.12 },
          {
            opacity: 1,
            stagger: 0.04,
            ease: "none",
            scrollTrigger: { trigger: q(".statement")[0], start: "top 78%", end: "center 45%", scrub: 0.5 },
          }
        );
        gsap.from(q(".state-chip"), {
          autoAlpha: 0,
          y: 20,
          stagger: 0.08,
          duration: 0.5,
          ease: "power3.out",
          scrollTrigger: { trigger: q(".state-chips")[0], start: "top 88%", once: true },
        });

        /* ---------- scroll: feature cards ---------- */
        gsap.set(q(".feat-card"), { autoAlpha: 0, y: 30 });
        ScrollTrigger.batch(q(".feat-card"), {
          start: "top 88%",
          once: true,
          onEnter: (els) =>
            gsap.to(els, { autoAlpha: 1, y: 0, stagger: 0.09, duration: 0.6, ease: "power3.out", overwrite: true }),
        });

        /* ---------- scroll: gate story plays like a movie ---------- */
        gsap.set(q(".gate-x"), { autoAlpha: 0, scale: 0.4 });
        gsap.set(q(".gate-comment, .gate-fix"), { autoAlpha: 0, y: 18 });
        gsap
          .timeline({
            defaults: { ease: "power3.out" },
            scrollTrigger: { trigger: q(".gate-demo")[0], start: "top 70%", once: true },
          })
          .to(q(".gate-spin"), { autoAlpha: 0, duration: 0.25, delay: 1.0 })
          .to(q(".gate-x"), { autoAlpha: 1, scale: 1, duration: 0.4, ease: "back.out(2.5)" }, "<")
          .to(q(".gate-comment"), { autoAlpha: 1, y: 0, duration: 0.5 }, "+=0.4")
          .to(q(".gate-fix"), { autoAlpha: 1, y: 0, duration: 0.5 }, "+=0.8");

        gsap.from(q(".yaml-line"), {
          autoAlpha: 0,
          x: -12,
          stagger: 0.05,
          duration: 0.4,
          ease: "power2.out",
          scrollTrigger: { trigger: q(".gate-code")[0], start: "top 78%", once: true },
        });
        gsap.from(q(".py-line"), {
          autoAlpha: 0,
          x: -12,
          stagger: 0.06,
          duration: 0.4,
          ease: "power2.out",
          scrollTrigger: { trigger: q(".sdk-code")[0], start: "top 80%", once: true },
        });

      });

      /* ---------- mouse parallax on the floating hero cards ---------- */
      mm.add("(prefers-reduced-motion: no-preference) and (pointer: fine)", () => {
        const hero = q(".hero-wrap")[0] as HTMLElement | undefined;
        const cards = q(".float-card") as HTMLElement[];
        if (!hero || !cards.length) return;
        const setters = cards.map((card, i) => ({
          x: gsap.quickTo(card, "x", { duration: 0.7, ease: "power3" }),
          y: gsap.quickTo(card, "y", { duration: 0.7, ease: "power3" }),
          depth: 12 + i * 8,
        }));
        const onMove = (e: PointerEvent) => {
          const r = hero.getBoundingClientRect();
          const nx = (e.clientX - r.left) / r.width - 0.5;
          const ny = (e.clientY - r.top) / r.height - 0.5;
          setters.forEach((s) => {
            s.x(-nx * 2 * s.depth);
            s.y(-ny * 2 * s.depth);
          });
        };
        hero.addEventListener("pointermove", onMove);
        return () => hero.removeEventListener("pointermove", onMove);
      });
    },
    { scope: rootRef }
  );

  return (
    <div ref={rootRef} className="overflow-x-clip">
      {/* ================================ nav ================================ */}
      <header className="site-nav fixed inset-x-0 top-0 z-50 transition-colors duration-300">
        <nav className="mx-auto flex h-16 max-w-[1200px] items-center justify-between px-6">
          <a href="#top" className="flex items-center gap-3">
            <Mark size={34} />
            <span className="leading-none">
              <span className="block whitespace-nowrap font-display text-lg font-bold tracking-tight">Tracely</span>
              <span className="mt-1 hidden whitespace-nowrap font-mono text-[9px] uppercase tracking-[0.22em] text-fg-faint sm:block">
                trace-native ci/cd
              </span>
            </span>
          </a>
          {/* Five, not eight. Replay/SDK/MCP still have their own sections and anchors — they were
              costing the bar more width than they earned, which is what wrapped the CTA onto two lines. */}
          <div className="hidden items-center gap-6 text-sm text-fg-muted lg:flex">
            <a className="whitespace-nowrap transition hover:text-fg" href="#loop">How it works</a>
            <a className="whitespace-nowrap transition hover:text-fg" href="#features">Features</a>
            <a className="whitespace-nowrap transition hover:text-fg" href="#gate">CI gate</a>
            <a className="whitespace-nowrap transition hover:text-fg" href="#pricing">Pricing</a>
            <a className="whitespace-nowrap transition hover:text-fg" href={DOCS} target="_blank" rel="noreferrer">Docs</a>
          </div>
          <div className="flex items-center gap-3">
            <a className={`${btnGhost} hidden px-4 py-2 sm:inline-flex`} href={GITHUB} target="_blank" rel="noreferrer">
              <IconGitHub className="h-4 w-4" /> GitHub
            </a>
            <a className={`${btnPrimary} px-4 py-2`} href={APP}>
              Open dashboard <IconArrow className="h-3.5 w-3.5" />
            </a>
          </div>
        </nav>
      </header>

      <main id="top">
        {/* ================================ hero ================================ */}
        <section className="hero-wrap relative overflow-hidden px-6 pb-24 pt-32">
          <div className="bg-blueprint pointer-events-none absolute inset-0" />
          <div className="pointer-events-none absolute inset-x-0 top-0 h-[760px] overflow-hidden [mask-image:radial-gradient(ellipse_78%_70%_at_50%_36%,#000_30%,transparent_76%)]">
            <div className="hero-dots absolute inset-0" />
          </div>
          <div
            className="pointer-events-none absolute inset-x-0 top-0 h-[520px]"
            style={{ background: "radial-gradient(640px 320px at 50% -8%, rgba(34,211,238,0.16), transparent 70%)" }}
          />

          <div className="relative mx-auto max-w-[1200px]">
            <div className="mx-auto max-w-5xl text-center">
              <div className="hero-stagger mx-auto flex max-w-md items-center gap-4">
                <div className="hairline-x flex-1" />
                <span className="eyebrow whitespace-nowrap">Trace-native CI/CD for AI agents</span>
                <div className="hairline-x flex-1" />
              </div>

              {/* Two hard lines, not a wrap: at any width between the clamp's ends the phrase
                  breaks where the meaning does, instead of orphaning "tests." on its own row. */}
              <h1 className="mt-7 font-display text-[clamp(34px,6.6vw,72px)] font-bold leading-[1.03] tracking-[-0.025em] text-fg">
                <span className="block">
                  <MaskWords text="Production failures" />
                </span>
                <span className="block">
                  <MaskWords text="become" />{" "}
                  <MaskWords text="regression tests." wordClass="text-gradient-cyan" />
                </span>
              </h1>

              <p className="hero-stagger mx-auto mt-6 max-w-2xl text-balance text-lg leading-relaxed text-fg-muted">
                LLM observability that closes the loop: every agent trace graded as it lands,
                failures clustered into issues, bad runs frozen into tests that block the PR.
              </p>

              <ScramblePipeline className="hero-stagger mt-5 block font-mono text-[13px] tracking-tight" />

              <div className="hero-stagger mt-8 flex flex-wrap items-center justify-center gap-4">
                <a className={btnPrimary} href={APP}>
                  Open the dashboard <IconArrow className="h-4 w-4" />
                </a>
                <a className={btnGhost} href={GITHUB} target="_blank" rel="noreferrer">
                  <IconGitHub className="h-4 w-4" /> Star on GitHub
                </a>
              </div>

            </div>

            {/* ------------------------- the self-writing trace ------------------------- */}
            <div className="relative mx-auto mt-20 max-w-5xl">
              {/* floating side cards */}
              <div className="float-card absolute -left-44 top-8 hidden w-56 -rotate-[5deg] p-4 glass xl:block">
                <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-fg-faint">hallucination · judge</p>
                <div className="mt-3 space-y-2">
                  {[["cited sources", "0.98"], ["grounded answer", "0.94"], ["no fabrication", "0.99"]].map(([k, v]) => (
                    <div key={k} className="flex items-center justify-between text-[11px]">
                      <span className="text-fg-muted">{k}</span>
                      <span className="rounded border border-ok/30 bg-ok-dim/60 px-1.5 py-0.5 font-mono text-ok">
                        {v} PASS
                      </span>
                    </div>
                  ))}
                </div>
              </div>
              <div className="float-card absolute -right-44 top-24 hidden w-60 rotate-[4deg] p-4 glass xl:block">
                <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-fg-faint">gate history · 7d</p>
                <div className="mt-3 flex items-center gap-1.5">
                  {["ok", "ok", "ok", "fail", "ok", "ok", "ok", "ok", "ok", "ok"].map((s, i) => (
                    <span key={i} className={`h-5 w-2 rounded-sm ${s === "ok" ? "bg-ok/70" : "bg-fail/80"}`} />
                  ))}
                </div>
                <p className="mt-3 text-[11px] text-fg-muted">
                  pass rate <span className="font-mono text-ok">96%</span> · 1 blocked merge
                </p>
              </div>

              {/* the frame */}
              <div className="hero-frame tr-frame relative glass text-left">
                <div className="flex items-center gap-2 border-b border-line/70 px-5 py-3">
                  <span className="h-2.5 w-2.5 rounded-full bg-line-bright" />
                  <span className="h-2.5 w-2.5 rounded-full bg-line-bright" />
                  <span className="h-2.5 w-2.5 rounded-full bg-line-bright" />
                  <span className="ml-3 truncate font-mono text-[11px] text-fg-faint">
                    tracely — traces / tr_9f2c41
                  </span>
                  <span className="ml-auto flex items-center gap-2 font-mono text-[10px] text-fg-faint">
                    <span className="rounded border border-line bg-ink-900 px-1.5 py-0.5">env=prod</span>
                    <span className="flex items-center gap-1.5">
                      <span className="h-1.5 w-1.5 animate-pulse2 rounded-full bg-fail" /> rec
                    </span>
                  </span>
                </div>

                <div className="p-5 sm:p-6">
                  <div className="space-y-1">
                    {ROWS.map((r) => (
                      <div
                        key={r.name}
                        className="tr-row grid grid-cols-[64px_minmax(0,130px)_1fr_44px] items-center gap-3 py-1.5 sm:grid-cols-[76px_minmax(0,170px)_1fr_52px]"
                      >
                        <span className={`font-mono text-[10px] font-semibold uppercase tracking-wider ${r.chip}`}>
                          {r.type}
                        </span>
                        <span className="truncate font-mono text-[12px] text-fg-muted">{r.name}</span>
                        <div className="relative h-5 rounded bg-ink-700/40">
                          <div
                            className={`tr-bar absolute top-1 h-3 rounded-[3px] ${r.bar} ${r.err ? "ring-1 ring-fail" : ""}`}
                            style={{ left: `${r.left}%`, width: `${r.w}%` }}
                          />
                          {r.err && (
                            <span
                              className="tr-err absolute -top-0.5 font-mono text-[9px] uppercase tracking-wider text-fail sm:text-[10px]"
                              style={{ left: `${r.left + r.w + 2}%` }}
                            >
                              ▲ timeout · unhandled
                            </span>
                          )}
                        </div>
                        <span className="text-right font-mono text-[11px] text-fg-faint">{r.dur}</span>
                      </div>
                    ))}
                  </div>

                  <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-line/60 pt-5">
                    <span className="mr-1 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-faint">
                      evaluators
                    </span>
                    <span className="tr-pill rounded-md border border-ok/30 bg-ok-dim/60 px-2 py-1 font-mono text-[11px] text-ok">
                      grounded · PASS
                    </span>
                    <span className="tr-pill rounded-md border border-ok/30 bg-ok-dim/60 px-2 py-1 font-mono text-[11px] text-ok">
                      refund_policy · PASS
                    </span>
                    <span className="tr-pill tr-pill-fail rounded-md border border-fail/40 bg-fail-dim/70 px-2 py-1 font-mono text-[11px] text-fail">
                      resolution · FAIL
                    </span>
                  </div>
                </div>

                {/* toasts */}
                <div className="pointer-events-none absolute bottom-4 right-4 flex w-[290px] max-w-[80%] flex-col items-end gap-2">
                  <div className="tr-toast tr-toast-cluster invisible w-full rounded-xl border border-line bg-ink-900/95 p-3 opacity-0 shadow-panel">
                    <div className="flex items-start gap-2.5">
                      <IconCluster className="mt-0.5 h-4 w-4 shrink-0 text-t_retriever" />
                      <div>
                        <p className="text-[12px] font-semibold text-fg">Cluster #12 — refund tool timeout ignored</p>
                        <p className="mt-0.5 font-mono text-[10px] text-fg-faint">23 traces this week · prod</p>
                      </div>
                    </div>
                  </div>
                  <div className="tr-toast tr-toast-case invisible w-full rounded-xl border border-signal/30 bg-ink-900/95 p-3 opacity-0 shadow-panel">
                    <div className="flex items-start gap-2.5">
                      <IconSnowflake className="mt-0.5 h-4 w-4 shrink-0 text-signal" />
                      <div>
                        <p className="text-[12px] font-semibold text-fg">Frozen as case rc_118</p>
                        <p className="mt-0.5 font-mono text-[10px] text-fg-faint">fixtures bundled · fail → pass contract</p>
                      </div>
                    </div>
                  </div>
                </div>

                {/* gate stamp */}
                <div className="tr-stamp invisible absolute inset-0 grid place-items-center opacity-0">
                  <div className="rounded-lg border-2 border-fail bg-ink-950/85 px-6 py-3.5 text-center shadow-panel backdrop-blur-sm">
                    <p className="font-mono text-lg font-bold tracking-[0.14em] text-fail sm:text-2xl">
                      GATE ✗ BLOCKED
                    </p>
                    <p className="mt-1 font-mono text-[11px] text-fg-muted">PR #214 reintroduces cluster #12</p>
                  </div>
                </div>
              </div>
            </div>

            {/* marquee */}
            <div className="hero-stagger mt-16">
              <p className="text-center font-mono text-[10px] uppercase tracking-[0.24em] text-fg-faint">
                instruments any agent stack
              </p>
              <div className="relative mt-5 overflow-hidden">
                <div className="pointer-events-none absolute inset-y-0 left-0 z-10 w-24 bg-gradient-to-r from-ink-950 to-transparent" />
                <div className="pointer-events-none absolute inset-y-0 right-0 z-10 w-24 bg-gradient-to-l from-ink-950 to-transparent" />
                <div className="flex w-max animate-marquee items-center gap-12">
                  {[...MARQUEE, ...MARQUEE].map((m, i) => (
                    <span key={i} className="whitespace-nowrap font-mono text-sm text-fg-muted/70">
                      {m}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* ================================ the loop ================================ */}
        <section id="loop" className="scroll-mt-24 px-6 py-20">
          <div className="mx-auto max-w-[1200px]">
            <Eyebrow>The loop</Eyebrow>
            <h2 className="sec-reveal mt-6 text-center font-display text-4xl font-bold tracking-tight sm:text-5xl">
              From production incident to CI gate.
            </h2>
            <p className="sec-reveal mx-auto mt-5 max-w-2xl text-center text-fg-muted">
              Four moves, no hand-authored datasets. Each one is derived from the last, and the trace
              starts it all.
            </p>

            <div className="loop-steps relative mt-16">
              <div className="loop-track absolute left-[12.5%] right-[12.5%] top-[43px] hidden lg:block">
                <svg className="h-[2px] w-full" viewBox="0 0 1000 2" preserveAspectRatio="none" aria-hidden>
                  <path d="M0 1 H1000" stroke="#28324a" strokeWidth="2" />
                  <path className="loop-path" d="M0 1 H1000" stroke="#22d3ee" strokeWidth="2" />
                </svg>
                <span className="loop-dot absolute -top-[5px] left-0 h-3 w-3 rounded-full bg-signal shadow-glow" />
              </div>
              <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
                {STEPS.map((s) => (
                  <div key={s.n} className="step-card relative z-10 rounded-2xl border border-line/80 bg-ink-800 p-6 shadow-frame">
                    <div className="grid h-10 w-10 place-items-center rounded-full border border-line-bright bg-ink-900 font-mono text-[11px] font-semibold text-signal">
                      {s.n}
                    </div>
                    <h3 className="mt-5 font-display text-xl font-bold">{s.title}</h3>
                    <p className="mt-2.5 text-sm leading-relaxed text-fg-muted">{s.body}</p>
                  </div>
                ))}
              </div>
            </div>

            <div className="sec-reveal mt-16">
              <PipelinePeek />
              <p className="mt-5 text-center font-mono text-[12px] text-fg-faint">
                one trace through the write path — ingest, grade, cluster, gate
              </p>
            </div>
          </div>
        </section>

        {/* ================================ replay ================================ */}
        <section id="replay" className="scroll-mt-24 px-6 py-20">
          <div className="mx-auto max-w-[1100px]">
            <Eyebrow>Watch the run</Eyebrow>
            <h2 className="sec-reveal mt-6 text-center font-display text-4xl font-bold tracking-tight sm:text-5xl">
              Your agents, acted out.
            </h2>
            <p className="sec-reveal mx-auto mt-5 max-w-2xl text-center text-fg-muted">
              Every conversation replays as a scene. Delegations walk over and talk, knowledge is read
              at the library, tools run at the wall — and the failure raises its hand where it happened.
            </p>
            <div className="sec-reveal mt-14">
              <FleetPeek />
            </div>
            <p className="sec-reveal mt-5 text-center font-mono text-[12px] text-fg-faint">
              a sample turn · every conversation in the dashboard replays like this — Sessions → Fleet
            </p>
          </div>
        </section>

        {/* ================================ statement ================================ */}
        <section className="statement px-6 py-20">
          <div className="mx-auto max-w-4xl text-center">
            <p className="font-display text-3xl font-bold leading-snug tracking-tight text-fg sm:text-[44px] sm:leading-[1.2]">
              <StateWords text="You never author a test set. Production already wrote the perfect failing example — Tracely freezes it and guards it forever." />
            </p>
            <div className="state-chips mt-12 flex flex-wrap items-center justify-center gap-3">
              {["1 click → frozen case", "$0 hermetic replay", "3 lines to instrument", "every PR gated"].map((t) => (
                <span
                  key={t}
                  className="state-chip rounded-full border border-line bg-ink-800/60 px-4 py-2 font-mono text-[12px] text-fg-muted"
                >
                  {t}
                </span>
              ))}
            </div>
          </div>
        </section>

        {/* ================================ features ================================ */}
        <section id="features" className="scroll-mt-24 px-6 py-20">
          <div className="mx-auto max-w-[1200px]">
            <Eyebrow>Derived from the trace</Eyebrow>
            <h2 className="sec-reveal mt-6 text-center font-display text-4xl font-bold tracking-tight sm:text-5xl">
              Everything downstream of one trace.
            </h2>
            <p className="sec-reveal mx-auto mt-5 max-w-2xl text-center text-fg-muted">
              Scores, clusters, cases, gates and trends are all computed from the trace — so they never
              disagree with each other, or with production.
            </p>
            <div className="mt-14 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
              {FEATURES.map((f) => (
                <div
                  key={f.title}
                  className="feat-card p-6 glass transition duration-300 hover:-translate-y-1 hover:border-signal/40"
                >
                  <div className="grid h-10 w-10 place-items-center rounded-lg border border-line bg-ink-900 text-signal">
                    <f.icon className="h-[18px] w-[18px]" />
                  </div>
                  <h3 className="mt-4 font-display text-lg font-bold">{f.title}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-fg-muted">{f.body}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* ================================ gate ================================ */}
        <section id="gate" className="scroll-mt-24 px-6 py-20">
          <div className="mx-auto max-w-[1200px]">
            <Eyebrow>Ship</Eyebrow>
            <h2 className="sec-reveal mt-6 text-center font-display text-4xl font-bold tracking-tight sm:text-5xl">
              The PR that re-breaks prod never merges.
            </h2>
            <p className="sec-reveal mx-auto mt-5 max-w-2xl text-center text-fg-muted">
              The gate replays your promoted cases on every pull request, exits non-zero on failure, and
              posts the verdict as a commit status and PR comment.
            </p>

            <div className="mt-14 grid items-start gap-6 lg:grid-cols-2">
              <div className="gate-code overflow-hidden glass">
                <div className="border-b border-line/70 px-5 py-3 font-mono text-[11px] text-fg-faint">
                  .github/workflows/tracely.yml
                </div>
                <pre className="overflow-x-auto p-5 font-mono text-[12.5px] leading-[1.75]">
                  {YAML_LINES.map((l, i) => (
                    <span key={i} className="yaml-line block">
                      {l}
                    </span>
                  ))}
                </pre>
              </div>

              <div className="gate-demo p-5 glass">
                <div className="flex items-baseline justify-between gap-3">
                  <p className="truncate text-sm font-semibold text-fg">fix: retry refund_api on timeout</p>
                  <p className="shrink-0 font-mono text-[11px] text-fg-faint">#214 · mira wants to merge 3 commits</p>
                </div>

                <div className="mt-4 rounded-xl border border-line bg-ink-900/60">
                  <div className="flex items-center gap-3 px-4 py-3.5">
                    <span className="relative h-5 w-5 shrink-0">
                      <IconSpinner className="gate-spin absolute inset-0 h-5 w-5 animate-spin text-warn" />
                      <IconX className="gate-x absolute inset-0 h-5 w-5 text-fail" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm text-fg">Tracely gate / gate (pull_request)</p>
                      <p className="font-mono text-[11px] text-fg-faint">Required · failing after 42s</p>
                    </div>
                    <span className="shrink-0 text-xs text-signal">Details</span>
                  </div>
                </div>

                <div className="gate-comment mt-4 rounded-xl border border-line bg-ink-900/60 p-4">
                  <div className="flex items-center gap-2">
                    <IconShield className="h-4 w-4 text-signal" />
                    <span className="font-mono text-xs text-fg">tracely-bot</span>
                    <span className="text-xs text-fg-faint">commented now</span>
                  </div>
                  <p className="mt-2.5 text-sm leading-relaxed text-fg-muted">
                    <span className="text-ok">11 passed</span> · <span className="text-fail">1 failed</span> — case{" "}
                    <span className="font-mono text-[12px] text-fg">rc_118</span> (cluster #12 · refund tool
                    timeout) regressed at step 4/6. Trajectory diff attached. <span className="text-fail">Merge blocked.</span>
                  </p>
                  <div className="mt-3 flex h-1.5 overflow-hidden rounded-full bg-ink-700">
                    <div className="w-[91%] bg-ok/70" />
                    <div className="w-[9%] bg-fail" />
                  </div>
                </div>

                <div className="gate-fix mt-4 flex items-center gap-3 rounded-xl border border-ok/30 bg-ok-dim/40 p-3.5">
                  <IconCheck className="h-5 w-5 shrink-0 text-ok" />
                  <div className="min-w-0">
                    <p className="truncate font-mono text-[11px] text-fg-muted">8f31c2a — await refund_api(order)</p>
                    <p className="text-sm text-ok">12/12 passed · gate green, merge unblocked</p>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* ================================ sdk ================================ */}
        <section id="sdk" className="scroll-mt-24 px-6 py-20">
          <div className="mx-auto grid max-w-[1200px] items-start gap-12 lg:grid-cols-2">
            <div>
              <div className="sec-reveal flex items-center gap-4">
                <span className="eyebrow whitespace-nowrap">Instrument</span>
                <div className="hairline-x flex-1" />
              </div>
              <h2 className="sec-reveal mt-6 font-display text-4xl font-bold tracking-tight sm:text-5xl">
                Three lines in.
                <br />
                Every span out.
              </h2>
              <p className="sec-reveal mt-5 max-w-xl leading-relaxed text-fg-muted">
                <span className="font-mono text-[13px] text-fg">tracely.init()</span> switches on
                auto-instrumentation for whatever your agent already uses — OpenAI, Anthropic,
                LangChain / LangGraph, LiteLLM — and stamps agent, conversation and turn onto every span.
                Zero span code in your codebase; manual context managers remain the escape hatch.
              </p>
              <div className="sec-reveal mt-7 flex flex-wrap gap-2.5">
                {["tokens & cost", "tools & retrievals", "thinking spans", "multi-turn conversations"].map((t) => (
                  <span key={t} className="rounded-full border border-line bg-ink-800/60 px-3.5 py-1.5 font-mono text-[11px] text-fg-muted">
                    {t}
                  </span>
                ))}
              </div>
              <div className="sec-reveal mt-9">
                <a className={btnGhost} href={DOCS} target="_blank" rel="noreferrer">
                  Read the SDK docs <IconArrow className="h-3.5 w-3.5" />
                </a>
              </div>
            </div>

            <div className="sdk-code overflow-hidden glass">
              <div className="flex items-center justify-between border-b border-line/70 px-5 py-3">
                <span className="font-mono text-[11px] text-fg-faint">agent.py</span>
                <span className="rounded border border-line bg-ink-900 px-2 py-0.5 font-mono text-[10px] text-fg-faint">
                  pip install &quot;tracely-ai[openai]&quot;
                </span>
              </div>
              <pre className="overflow-x-auto p-5 font-mono text-[12.5px] leading-[1.8]">
                {PY_LINES.map((l, i) => (
                  <span key={i} className="py-line block">
                    {l}
                  </span>
                ))}
              </pre>
            </div>
          </div>
        </section>

        {/* =================================== mcp =================================== */}
        <section id="mcp" className="scroll-mt-24 px-6 py-20">
          <div className="mx-auto grid max-w-[1200px] items-start gap-12 lg:grid-cols-2">
            <div>
              <div className="sec-reveal flex items-center gap-4">
                <span className="eyebrow whitespace-nowrap">MCP</span>
                <div className="hairline-x flex-1" />
              </div>
              <h2 className="sec-reveal mt-6 font-display text-4xl font-bold tracking-tight sm:text-5xl">
                Ask your editor
                <br />
                what broke.
              </h2>
              <p className="sec-reveal mt-5 max-w-xl leading-relaxed text-fg-muted">
                Every Tracely deployment is also an MCP server. Point Claude Code or Cursor at it and
                your coding agent reads the failing traces, opens the cluster behind them, and writes
                the evaluator that catches it next time — authenticated by an ordinary ingest key,
                scoped to that one workspace. Nothing to install, nothing extra to run.
              </p>
              <div className="sec-reveal mt-7 flex flex-wrap gap-2.5">
                {["read traces", "inspect clusters", "create evaluators", "trends & cost"].map((t) => (
                  <span key={t} className="rounded-full border border-line bg-ink-800/60 px-3.5 py-1.5 font-mono text-[11px] text-fg-muted">
                    {t}
                  </span>
                ))}
              </div>
              <div className="sec-reveal mt-9">
                <a className={btnGhost} href={`${DOCS}/mcp`} target="_blank" rel="noreferrer">
                  Connect your agent <IconArrow className="h-3.5 w-3.5" />
                </a>
              </div>
            </div>

            <div className="sec-reveal overflow-hidden glass">
              <div className="flex items-center justify-between border-b border-line/70 px-5 py-3">
                <span className="font-mono text-[11px] text-fg-faint">terminal</span>
                <span className="rounded border border-line bg-ink-900 px-2 py-0.5 font-mono text-[10px] text-fg-faint">
                  11 tools · streamable HTTP
                </span>
              </div>
              <pre className="overflow-x-auto p-5 font-mono text-[12.5px] leading-[1.8]">
                {MCP_LINES.map((l, i) => (
                  <span key={i} className="block">
                    {l}
                  </span>
                ))}
              </pre>
            </div>
          </div>

        </section>

        {/* the know-how half — MCP hands the agent your data, the skill hands it the know-how */}
        <section className="px-6 py-20">
          <div className="sec-reveal mx-auto grid max-w-[1200px] items-start gap-12 lg:grid-cols-2">
            <div>
              <div className="flex items-center gap-4">
                <span className="eyebrow whitespace-nowrap">Skill</span>
                <div className="hairline-x flex-1" />
              </div>
              <h3 className="mt-6 font-display text-2xl font-bold tracking-tight sm:text-3xl">
                MCP gives it your data.
                <br />
                The skill gives it the know-how.
              </h3>
              <p className="mt-5 max-w-xl leading-relaxed text-fg-muted">
                One command and your coding agent knows how Tracely actually works — zero-span-code
                instrumentation, the manual span API, evaluator design, the PR gate, and the handful
                of conventions that fail <em>silently</em> when you get them wrong.
              </p>
              <div className="mt-9">
                <a className={btnGhost} href="/agent-skill">
                  What&apos;s in the skill <IconArrow className="h-3.5 w-3.5" />
                </a>
              </div>
            </div>

            <div className="overflow-hidden glass">
              <div className="flex items-center justify-between border-b border-line/70 px-5 py-3">
                <span className="font-mono text-[11px] text-fg-faint">terminal</span>
                <span className="rounded border border-line bg-ink-900 px-2 py-0.5 font-mono text-[10px] text-fg-faint">
                  6 files · loaded on demand
                </span>
              </div>
              <pre className="overflow-x-auto p-5 font-mono text-[12.5px] leading-[1.8]">
                {SKILL_LINES.map((l, i) => (
                  <span key={i} className="block">
                    {l}
                  </span>
                ))}
              </pre>
            </div>
          </div>
        </section>

        {/* ================================= pricing ================================= */}
        <section id="pricing" className="scroll-mt-24 px-6 py-20">
          <div className="mx-auto max-w-[1200px]">
            <div className="sec-reveal flex items-center gap-4">
              <span className="eyebrow whitespace-nowrap">Pricing</span>
              <div className="hairline-x flex-1" />
            </div>
            <h2 className="sec-reveal mt-6 max-w-3xl font-display text-4xl font-bold tracking-tight sm:text-5xl">
              Free to self-host.
              <br />
              Free to start hosted.
            </h2>
            <p className="sec-reveal mt-5 max-w-2xl leading-relaxed text-fg-muted">
              The whole product is MIT-licensed — API, worker, UI, evaluators, the CI gate. Run it
              yourself and pay nobody. The hosted plan exists so you don&apos;t have to run
              ClickHouse.
            </p>

            <div className="mt-14 grid gap-6 lg:grid-cols-3">
              {PLANS.map((plan) => (
                <div
                  key={plan.name}
                  className={`sec-reveal glass relative flex flex-col rounded-2xl p-7 ${
                    plan.featured ? "border-signal/40 shadow-glow" : ""
                  }`}
                >
                  {plan.featured && (
                    <span className="absolute -top-3 left-7 rounded-full bg-signal px-3 py-1 font-mono text-[10px] font-semibold uppercase tracking-wider text-ink-950">
                      Most popular
                    </span>
                  )}
                  <h3 className="font-display text-xl font-bold tracking-tight">{plan.name}</h3>
                  <p className="mt-2 min-h-[40px] text-[13.5px] leading-relaxed text-fg-muted">
                    {plan.blurb}
                  </p>
                  <div className="mt-6 flex items-baseline gap-1.5">
                    <span className="font-display text-4xl font-extrabold tracking-tight">
                      {plan.price}
                    </span>
                    {plan.per && <span className="text-[13px] text-fg-faint">{plan.per}</span>}
                  </div>
                  <ul className="mt-7 flex-1 space-y-2.5">
                    {plan.features.map((f) => (
                      <li key={f} className="flex gap-2.5 text-[13.5px] leading-relaxed text-fg-muted">
                        <IconCheck className="mt-[3px] h-3.5 w-3.5 flex-none text-signal" />
                        <span>{f}</span>
                      </li>
                    ))}
                  </ul>
                  <a
                    className={`${plan.featured ? btnPrimary : btnGhost} mt-8 justify-center`}
                    href={plan.href}
                    {...(plan.href.startsWith("http") ? { target: "_blank", rel: "noreferrer" } : {})}
                  >
                    {plan.cta} <IconArrow className="h-3.5 w-3.5" />
                  </a>
                </div>
              ))}
            </div>

            <p className="sec-reveal mt-10 max-w-3xl text-[13px] leading-relaxed text-fg-faint">
              <span className="text-fg-muted">Bring your own model key.</span> LLM judges run on
              your OpenRouter key, scoped to your workspace and encrypted at rest — we never bill
              you a markup on inference, and we never use a shared key. No key configured? The
              structural evaluators still grade every run; the LLM ones switch off cleanly.
            </p>
          </div>
        </section>

        {/* ================================ final cta ================================ */}
        <section id="start" className="final-cta relative scroll-mt-24 overflow-hidden px-6 pb-10 pt-20">
          <div className="bg-blueprint pointer-events-none absolute inset-0" />
          <div
            className="pointer-events-none absolute inset-x-0 bottom-0 h-[420px]"
            style={{ background: "radial-gradient(720px 360px at 50% 108%, rgba(34,211,238,0.13), transparent 70%)" }}
          />
          <div className="relative mx-auto max-w-[1200px] text-center">
            <div className="sec-reveal mx-auto flex max-w-md items-center gap-4">
              <div className="hairline-x flex-1" />
              <span className="eyebrow whitespace-nowrap">Start in one command</span>
              <div className="hairline-x flex-1" />
            </div>
            <h2 className="sec-reveal mx-auto mt-8 max-w-4xl text-balance font-display text-[clamp(32px,4.6vw,54px)] font-bold leading-[1.06] tracking-[-0.025em] text-fg">
              Ship agents that <span className="text-gradient-cyan">don&apos;t regress.</span>
            </h2>
            <p className="sec-reveal mx-auto mt-6 max-w-2xl text-balance leading-relaxed text-fg-muted">
              Instrument an agent in two lines, send one trace, and the loop starts on its own —
              graded on arrival, clustered when it fails, frozen into a case that guards the next PR.
            </p>
            <div className="sec-reveal mt-8 flex justify-center">
              <CopyCmd cmd={INSTALL} />
            </div>
            <div className="sec-reveal mt-6 flex flex-wrap items-center justify-center gap-4">
              <a className={btnPrimary} href={APP}>
                Open the dashboard <IconArrow className="h-4 w-4" />
              </a>
              <a className={btnGhost} href={GITHUB} target="_blank" rel="noreferrer">
                <IconGitHub className="h-4 w-4" /> Star on GitHub
              </a>
            </div>

            <p className="sec-reveal mt-12 pb-16 text-sm text-fg-faint">
              Built and maintained by{" "}
              <a
                className="font-medium text-fg-muted underline decoration-line-bright underline-offset-4 transition hover:text-signal"
                href={LINKEDIN}
                target="_blank"
                rel="noreferrer"
              >
                Julien Wuthrich
              </a>
            </p>

            <footer className="border-t border-line/60 pt-8 pb-10 text-left">
              <div className="flex flex-col items-center justify-between gap-5 sm:flex-row">
                <p className="flex items-center gap-2.5 text-sm text-fg-faint">
                  <Mark size={22} />
                  <span className="font-display font-bold text-fg-muted">Tracely</span> · © 2026
                </p>
                <div className="flex items-center gap-6 text-sm text-fg-muted">
                  <a className="transition hover:text-fg" href={DOCS} target="_blank" rel="noreferrer">Docs</a>
                  <a className="transition hover:text-fg" href={GITHUB} target="_blank" rel="noreferrer">GitHub</a>
                  <a className="transition hover:text-fg" href={LINKEDIN} target="_blank" rel="noreferrer">LinkedIn</a>
                </div>
              </div>
              {/* Internal links are how crawl budget and authority reach new content pages, so they
                  stay — demoted to their own quiet row rather than crowding the primary one.
                  Add every new marketing route here as well as to app/sitemap.ts. */}
              <div className="mt-6 flex flex-wrap items-center justify-center gap-x-5 gap-y-2 text-[12.5px] text-fg-faint sm:justify-start">
                <a className="transition hover:text-fg-muted" href="/llm-evaluation">LLM evaluation</a>
                <a className="transition hover:text-fg-muted" href="/llm-as-a-judge">LLM-as-a-judge</a>
                <a className="transition hover:text-fg-muted" href="/langfuse-alternatives">Langfuse alternatives</a>
                <a className="transition hover:text-fg-muted" href="/agent-skill">Agent skill</a>
              </div>
            </footer>
          </div>
        </section>
      </main>
    </div>
  );
}
