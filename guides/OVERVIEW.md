# 🛰️ Tracely — The Complete Guide

> **Trace-native CI/CD for AI agents.** Your agents' production traces become regression tests that block bad pull requests — automatically detected, intelligently grouped, frozen with one click, and replayed for free on every PR.

`v0.1.0 · MVP` • A living overview of what Tracely is, every interesting feature, why it's built this way, and what's next.

---

## 📑 Contents

1. [🧠 The big idea (ELI5)](#-the-big-idea-eli5)
2. [🎯 What Tracely is — and isn't](#-what-tracely-is--and-isnt)
3. [🦴 The spine + the product map](#-the-spine--the-product-map)
4. [🏗️ Architecture](#️-architecture)
5. [📚 The features](#-the-features)
   - [👀 Observe](#-observe--see-everything-your-agents-do) · [🔬 Detect](#-detect--grade-every-run) · [⚖️ Judge calibration](#-judge-calibration--trust-your-evaluators-before-they-block-ci) · [🗂️ Conversation context](#-conversation-context--rolling-summary--declared-agents) · [🧹 Triage](#-triage--group-failures-into-issues) · [🧪 Test](#-test--freeze-a-failure-into-a-regression) · [🚢 Ship](#-ship--gate-the-pull-request) · [📈 Insights](#-insights--trends--meta-analysis) · [🎨 The UI](#-the-ui)
6. [🔑 Key decisions](#-key-decisions--why)
7. [🐛 War stories](#-war-stories-bugs-we-hunted)
8. [⚠️ Honest limitations](#️-honest-limitations)
9. [🚀 What's next](#-whats-next)
10. [🏃 Run it](#-run-it) · [🗂️ Codebase map](#️-codebase-map)

---

## 🧠 The big idea (ELI5)

> 🍼 Imagine a robot that answers customer questions and uses tools (look up an order, check the weather). One day it quietly breaks — it *says* it checked your order but never actually called the tool, and makes up an answer. 😱
>
> Most tools draw a chart that says "1 bad thing happened." Tracely does better: it **records that exact broken moment**, turns it into a **test** with one click, and from then on **re-plays that moment on every code change** — cheaply, offline, deterministically. If a change would bring the bug back, Tracely 🛑 **blocks the pull request** before it ships.

> 💡 **The recorded run *is* the test.** You never hand-author a dataset of example questions and ideal answers. Production already handed you the perfect failing example — Tracely just freezes it and guards against it forever.

Everything else — quality scores, failure clusters, suggested fixes, CI verdicts, trends — is **derived from the trace**. The trace is the source of truth. 🌱

---

## 🎯 What Tracely is — and isn't

| ✅ Tracely IS | ❌ Tracely is NOT |
|---|---|
| **Trace-native** — the production trace is the primary key | ❌ another Langfuse (it doesn't stop at "pretty traces") |
| **Agent-first** — agent / run / conversation / turn / tool are first-class | ❌ prompt management (the versioned thing is the *agent*) |
| **Regression-first** — tests are born from real failures | ❌ dataset-first eval (you never author `{input, expected}` up front) |
| **A CI gate** — produces a PASS/FAIL that blocks a PR | ❌ Datadog-for-LLMs (it's CI, not metric dashboards) |

> 🧭 **The wedge:** every incumbent (LangSmith, Braintrust, Phoenix, Galileo…) bottoms out in `Dataset → Experiment → Scorer`, which throws away the trajectory, the tool outputs, and the agent version that failed. Making the **trace** the literal primary key is the defensible white space.

---

## 🦴 The spine + the product map

One closed loop. The sidebar teaches it as four stages (+ Insights):

```
   👀 OBSERVE        🔬 DETECT         🧹 TRIAGE        🧪 TEST           🚢 SHIP
 ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
 │Production│───▶│ Failure  │───▶│ Failure  │───▶│Regression│───▶│  CI/CD   │
 │  Trace   │    │Detection │    │Clustering│    │   Test   │    │   Gate   │
 └──────────┘    └──────────┘    └──────────┘    └──────────┘    └────┬─────┘
       ▲                                                              │
       └──────────────── 🔁 a blocked-then-fixed PR ships green ──────┘
```

| Stage | Sidebar nav | What it does |
|---|---|---|
| 👀 **Observe** | *Traces · Trends · Dashboard* | OTLP traces, grouped into **conversation threads**, with a tabbed detail view |
| 🔬 **Detect** | *(automatic)* | Every run auto-graded — incl. **silent** failures |
| 🧹 **Triage** | *Failure clusters* | Failures grouped into named **Issues** (Engine-style) |
| 🧪 **Test** | *Regression cases · Judge calibration* | One failure → a fail-to-pass test with hermetic fixtures; ✓agree/✗disagree with judge verdicts |
| 🚢 **Ship** | *CI gates* | A PR replays the suite → a blocking green/red check (with judge-in-gate + `NO_COVERAGE`) |
| 📈 **Insights** | *Trends* | Failure-rate, gate pass-rate, MTTR over time + per-agent **Meta-Analysis** ("Analyze") cross-metric correlations |

Plus a **⌘K command palette** to jump anywhere.

---

## 🏗️ Architecture

> 🍼 A little team of helpers: a **doorman** (API) catches traces and stuffs the raw envelope in a **safe** (S3) so nothing is ever lost, then drops a ticket in a **queue** (Redis). A **back-office worker** files each step into a **fast filing cabinet** (ClickHouse) and grades it. A **notebook** (Postgres) tracks agents, tests, gates, evaluators. A **website** shows it all.

```
  your agent (Tracely SDK / any OTLP exporter)
                │  POST /v1/traces
                ▼
  FastAPI backend ── write raw OTLP to S3 (source of truth) ──▶ 🪣 MinIO/S3
        │          ── enqueue ───────────────────────────────▶ 🔴 Redis
        ▼  Celery worker (same package)
  parse → resolve agent → insert spans ─▶ 🧱 ClickHouse (events + scores)
        │  then auto-evaluate (debounced) ─▶ 🐘 Postgres+pgvector (registry, cases, gates, clusters, evaluators)
        ▼
  Next.js 15 frontend (SSR, no-store)
```

| Layer | Tech | Why |
|---|---|---|
| API + worker | **Python · FastAPI · Celery** | one shared `tracely` package — API (producer) & worker (consumer) never drift |
| Hot store | **ClickHouse** (`ReplacingMergeTree`, read `FINAL`) | millions of spans; one row per span; upserts late/duplicate spans |
| Registry | **Postgres 17 + pgvector** | agents, cases, gates, clusters, evaluators + cached failure embeddings |
| Blob | **MinIO / S3** | raw OTLP + replay fixtures = durable source of truth |
| Queue | **Redis** | Celery broker + result backend |
| UI | **Next.js 15 · React 19 · Tailwind** (zero UI libs) | a distinctive, hand-rolled dark dashboard |
| SDK + CLI | **`tracely_sdk`** (OpenTelemetry) | instrument agents + the `tracely` CI command |

> ⚡ **Dev loop:** backend/worker/migrate/frontend bind-mount the source and the Python packages are `uv`-editable — edit a file, restart the one service, no image rebuild. Whole stack: `docker compose up`.

---

## 📚 The features

### 👀 Observe — see everything your agents do

**Ingestion & data model ✅**
- **`POST /v1/traces`** accepts OTLP protobuf *or* JSON from any OTel SDK. **Blob-first**: raw bytes hit S3 *before* the queue, so nothing is ever lost.
- One row per span in ClickHouse `events`, with **first-class agent columns** (`agent_id`, `agent_run_id`, `conversation_id`, `turn_id`, typed caller/callee edges) + provenance (`evaluation_case_id`, `gate_run_id`, `failure_cluster_id`) + model/usage/cost.
- **Multi-vendor mapping** — lifts OpenInference, OTel GenAI, Langfuse, LangGraph, and Tracely-native attributes into the *same* typed columns. "Which level does a score target?" is a **column lookup**, not a read-time parse — the core edge over generic span storage.

**🧵 Conversation threads ✅** *(new)*
- The Traces page is no longer a flat list — traces **group into threads by conversation/session**, showing 👤 **first user message → 🤖 last assistant answer**, a turns badge, tokens, status, and time (LangSmith-style). Proper two-level grouping (resolve each trace's conversation from its root span, then group).
- **Filters + search** on the thread list (all / failing / multi-turn + free text), and a **⌘K command palette** to jump to any trace/case/cluster/gate.
- A **thread detail** page = a conversation replay: each turn shows the messages **and its full waterfall** (tool calling), with metrics and an "open trace →" link.
- The SDK takes `conversation=` on `agent()` to thread runs together.

**🌊 Tabbed trace detail ✅** *(new)*
- Opens on the **Trace** tab — a flamegraph **waterfall** (color-coded by span type, staggered animation) + a **tabbed span inspector**: **Input · Output · Attributes**. The Attributes tab is a full metadata table (gen_ai.*, tracely.*, model, env…).
- **Evaluations** live in their *own tab* (not cluttering the trace), with a verdict badge on the tab so you know if anything failed.
- Input/Output render as a **conversation/markdown view** (chat bubbles, collapsible JSON) instead of a raw `<pre>` dump. Tokens & cost roll up to the trace header.

`api/routers/reads.py` · `otel/mapping.py` · `components/{ThreadList,TraceBody,Waterfall,IO}.tsx` · `sdk/tracely_sdk`

---

### 🔬 Detect — grade every run

> 🍼 The moment a run lands, it's auto-graded (debounced ~4s so late spans settle). The grades become pass/fail **scores**.

**The checks ✅** (run automatically today):

| Check | Catches |
|---|---|
| **Run outcome** | any errored span 💥 |
| **Tool success** | a specific tool span that errored (pinpointed) |
| **Tool consistency** | 🥷 **silent failure** — a tool *requested* but never executed |
| **Latency** | over the latency budget |
| **Answer quality (LLM judge)** | wrong/unfaithful answers — grades the agent's *real* answer against its tool results (needs a key) |
| **Conversation intent (LLM judge)** | labels each turn — `greeting → faq_question → checkout` — chained, so every turn is read against the intents before it (needs a key) |

> 🥷 **Silent failures are the star** — a run with zero error spans can still be broken (the model claims it called a tool but didn't, then hallucinates). No structural-only tool catches this.

**✅ User-owned evaluators (live)** — the engine runs **configurable `Evaluator` records** (`models.Evaluator`, migration `0007`): each has a kind (`structural` | `llm_judge`), target agent/env, sampling, an **`advisory` flag**, and config. The runner loads the project's enabled records, with **deterministic per-(trace,score) sampling** (so the same trace yields the same decision across re-runs) and **real target filtering** (`target_agent` / `target_env` actually filter on the ingest path). The seeder installs the recommended template catalog as editable rows; the **Add Column modal** + Columns menu in the table edit them inline.

**🔍 Multi-level evaluation ✅** — each evaluator is scoped at one of three levels (config JSON):
- **`SPAN`** — graded per-step (a tool span, a generation).
- **`AGENT_RUN`** — graded per trace (the run-level checks above).
- **`CONVERSATION`** — graded across an entire thread (multi-turn quality, conversation-level invariants). Stored on `scores.session_id` (no trace_id) — one canonical row per thread per evaluator.

**📜 Advisory vs blocking ✅** — the answer-quality judge is intentionally subjective; marked **advisory** (per-evaluator flag, migration `0012`), its FAILs are kept as signal but **do not flip** the roll-up `failing` flag. One policy in `domain/evaluation/verdict.py` — every roll-up (thread dot, trace verdict, session verdict, analytics counters) reads from the same source. End of the "green dot but EVALS FAIL" inconsistency.

**🧪 Calibrate before you trust ✅** — let an LLM judge block CI only after you've checked it agrees with you. The **Judge calibration** page lets a reviewer ✓agree / ✗disagree with each judge verdict; agreement %, **missed failures** (false_pass) and **over-flags** (false_fail) surface per evaluator (see [Judge calibration](#judge-calibration-trust-your-evaluators-before-they-block-ci) below).

`evaluators.py` · `eval_runner.py` · `domain/evaluation/{verdict,calibration}.py` · scores → ClickHouse `scores` (deterministic ids → idempotent re-eval)

---

### ⚖️ Judge calibration — trust your evaluators before they block CI

> 🍼 Letting an LLM judge fail your CI is scary. Tracely lets you ✓agree or ✗disagree with each judge verdict, then tells you how often the judge matches a human — and which way it errs.

**What's built ✅**
- **Human-label write path** — Postgres `score_annotations` (migration `0013`), keyed by the score's **natural identity** + reviewer (`score_name, evaluation_level, trace_id, session_id, observation_id, labeled_by`). One label per reviewer per score, upserted. We **snapshot the judge verdict at label time** so agreement is a pure Postgres query (no ClickHouse join) and reflects what the human actually reviewed.
- **`/calibration` page** — left rail = evaluator agreement cards (colored % — ok≥0.8 / warn≥0.5 / fail); right rail = the labeling queue with the judge's verdict + rationale + ✓agree / ✗disagree toggle.
- **The two metrics that matter** — **`false_pass` (missed failures)**: judge passed a trace the human would fail (the dangerous one for a gate); **`false_fail` (over-flags)**: judge failed something the human says is fine (noisy gate).
- **Pure math** — `domain/evaluation/calibration.py` is fully unit-tested; the router shapes HTTP only.

> 🧭 **Why this is the moat-widener:** every competitor ships LLM judges and a gate. Tracely is the only one that says "let's check the judge against you first." Trustworthy evals = a gate teams will actually enable.

`api/routers/calibration.py` · `domain/evaluation/calibration.py` · `components/CalibrationView.tsx`

---

### 🗂️ Conversation context — rolling summary + declared agents

> 🍼 A 30-turn conversation is too long for any single judge call. Tracely keeps a **rolling summary** that accumulates per-span (verbatim for short steps, LLM-compressed for long ones), and lets the user **declare their agent catalog** so the judge sees "Support Agent uses get_order_status" instead of just span ids.

**Rolling summary ✅** *(migration `0010`)* — per-span accumulating summary stored at the step level (one row per span = the full history-so-far at that point), so the conversation view = the last row, the message view = its turn's last step, the step view = that exact row. Algorithm:
- **RULE 1** — a step ≤ `step_max_tokens` (default 512) is kept **verbatim**; larger steps are summarized to ~10-20 words by a small model.
- **RULE 2** — when the running list exceeds `max_tokens` (default 16k, editable per workspace from the Rolling summary column), the older items (all but the last 2) **fold into one item with the `prev_summary` role**, recursively. The summary stays a flat list `[{role, type, content, …}]` — `prev_summary` is a *role*, not a key, so renderers and judges treat it uniformly.
- Auto-generates on ingest. Surfaced as **3 per-row columns** in the trace table (C / M / S levels) rendered as JSON pills; available to advanced evaluators as **`@HISTORY` / `@ROLLING_SUMMARY`** template variables.

**Conversation agents panel ✅** *(migration `0011`)* — two sources, merged into one panel:
1. **Observed** — derived from spans (`agent_id` + executed TOOL spans + `tool_call_names` for requested-but-not-executed).
2. **Declared** — the user sends a rich catalog via `tracely.trace(agents=[{name, description, tools: {...}}])` on the first turn; the ingestion service parses + upserts to `conversation_agents` and **strips it from metadata** before the ClickHouse insert (lossless-metadata path, no mapper change).

The judge reads `@AGENTS`: the **declared** catalog merged with what the traces show — tools the model was offered and tools it called, each marked by source — a major quality win for multi-agent traces.

`services/rolling_summary_service.py` · `domain/evaluation/rolling_summary.py` · `components/AgentsSidePanel.tsx`

---

### 🧹 Triage — group failures into Issues

> 🍼 When 50 runs break, are they really the same 3 bugs? Tracely groups them — instantly by a fingerprint, then (on demand) with real AI that writes a plain-English **Issue**.

**Two clustering systems ✅**
1. **Signature clustering** — instant, free, structural fingerprint of each failure (so the Failures screen is never empty).
2. **Embedding + LLM clustering** ("Analyze failures") — embeds a **mechanism-focused** signature → cosine/HDBSCAN clusters (UMAP only at scale) → a per-cluster **LangChain `create_agent`** writes a semantic title/description/severity/fix → a **meta-consolidation agent** merges/splits into clean Issues. Grounded in the actual evaluator verdicts + tool results.

**🔬 Engine-style cluster detail ✅** *(new — LangSmith Engine parity)*
- An **occurrence histogram** (when this failure happened, over time).
- **Linked traces** — each member shows its 👤 input snippet + latency.
- A **suggested evaluator with copyable code** — a real detector keyed to the failure mechanism (e.g. for "tool not executed" it generates the `def evaluate(ctx)` that checks requested-vs-executed tools), matching the actual evaluator interface.
- One-click **Promote** → a regression test; **Ignore** to dismiss. Re-analysis **inherits** your promotions so they're never lost.

`fi.py` · `agents.py` · `cluster.py` · `api/routers/clusters.py` · `clusters/[clusterId]/page.tsx`

---

### 🧪 Test — freeze a failure into a regression

> 🍼 Click **Promote** on a bad run → it becomes a rule: "given this input, never fail this way again, and still use the right tools." The test starts **red** (it fails on the run it came from) and only goes **green** once truly fixed.

**What's built ✅**
- **`promote_trace`** — one trace → a durable `EvaluationCase`, idempotent (keyed by `input_digest`, so one flaky bug can't spawn 10,000 cases). Captures a reference trajectory, assertions, and **fixtures**.
- **Fail-to-pass contract** 🔴→🟢, validated at promote time — a case becomes PROMOTED only if the broken run genuinely fails it.
- **Trajectory match modes** (agentevals-style: superset/strict/unordered/subset).
- **🔒 Faithful hermetic fixtures** *(new)* — fixtures are now an **ordered list of calls**, each with its **args, output, AND error status**. The SDK's `call_tool(name, fn, args=…)` / `call_llm` serve them in order (or by args), and **raise `ToolError`** when the recorded call errored — so a tool that timed out in production replays as a timeout, and the gate reproduces the *exact* failure. (Before, both calls got one output and the error was silently dropped → a spurious PASS.)
- **🛡️ Run-outcome assertion** *(new)* — `allow_tool_errors`: a tool *may* fail (it's the replayed environment) as long as the agent's own run is clean — so a graceful **error-handling fix can pass** while a crashing agent still fails. Auto-set when the source failed because a tool errored *and* the agent crashed.

`regression.py` · `trajectory.py` · `api/routers/cases.py` · `sdk/tracely_sdk` (`call_tool`/`call_llm`/`fixtures`)

---

### 🚢 Ship — gate the pull request

> 🍼 On every PR, the `tracely` CLI re-runs your agent against all the saved failures and **blocks the merge** if any regress — posting a green/red check + comment right on the PR. For free, by replaying recorded tool/LLM outputs so CI never calls a real model.

**What's built ✅**
- **`tracely` CLI** (ships in the SDK, stdlib-only): `tracely simulate` (drive each agent's scenarios against its registered HTTP endpoint — one agent, a subset, or `--all`; needs no agent code in CI), `tracely gate` (match pre-emitted ci traces by digest) and `tracely replay` (re-run your agent on each promoted case, then gate).
- **Explicit pairing** — replay knows which trace ran which case, so it gates with an exact `{case_id: trace_id}` map (no digest guessing).
- **Hermetic by default** — fixtures served via `call_tool`/`call_llm`; `--live` opts into real calls.
- **GitHub PR check** — a commit status `tracely/regression-gate` (the blocking check) + an upserted PR comment, exit codes `0/1/2`. A composite **GitHub Action** + example workflow ship in `.github/`.
- **💸 Soft delta gates** — the gate rolls up candidate **latency** and **token usage**, compares to the agent's last **green** gate, and posts non-blocking **⚠️ warnings** when a metric regresses (default 25% thresholds). Metrics + warnings persist on `GateRun` (migration `0006`) and render in the CLI, the PR comment, and the gate UI.

**🧑‍⚖️ Honest gate (no more false-green)** *(new — strategic capstone)*
- **`NO_COVERAGE` status** — a gate that exercised NONE of its promoted cases (every case SKIPped) is treated as a blocking non-PASS, not green. A merge-blocker that tested nothing is the worst possible failure mode. Set `gate_require_full_coverage=true` to extend this to partial coverage too.
- **Judge in the gate** — cases promoted from an *answer-quality* failure (a hallucination with a structurally-clean trace) carry a `quality` assertion. At gate time the answer judge re-grades the replayed answer; a sub-threshold score **FAILs the case**. This is what turns the gate from "catches crashes" into "catches the bad answers customers fear". Gated by `settings.gate_quality_blocks` (default on) — set false to keep it advisory while you calibrate (see [Judge calibration](#judge-calibration-trust-your-evaluators-before-they-block-ci)). The canonical judge is `tracely.run.quality`.

`gate.py` · `domain/regression/contract.py` · `api/routers/gate.py` · `sdk/tracely_sdk/cli.py` · `.github/actions/tracely-gate/`

---

### 📈 Insights — Trends + Meta-analysis

**Trends ✅** — `GET /api/trends` + a `/trends` page: **failure-rate over time, gate pass-rate, open-vs-resolved issues, regression-test count, and an MTTR (failure → test) proxy**, with hand-rolled bar charts. Scoped to **regression-loop health** (on-thesis), not generic Datadog-style metrics.

**🔬 Meta-Analysis ("Analyze") ✅** *(migration `0009`)* — cross-metric **Spearman correlations + z-score outliers** computed deterministically in NumPy (tie-averaged ranks → Pearson; reports sample-size `n`, not a fake p-value), then an LLM **synthesis** call merged with the stats (stats stay authoritative; the LLM contributes interpretations, patterns, recommendations, summary). Scoped **per agent**, persisted in Postgres `meta_analyses`. Shows up as the `MetaAnalysisPanel` on the Trends page — re-runs on demand, exportable as Markdown.

`api/routers/analytics.py` · `api/routers/meta_analysis.py` · `domain/analysis/statistics.py` · `trends/page.tsx` · `components/{Bars,MetaAnalysisPanel}.tsx`

---

### 🎨 The UI

Hand-rolled Next.js 15 dashboard, **zero UI libraries** (Tailwind + clsx + inline SVG icons). Near-black `ink` palette, **cyan "signal"** accent, faint grid, glow, staggered reveals; custom fonts (Bricolage Grotesque / Hanken Grotesk / JetBrains Mono). Every page is a **server component** fetching the backend with `cache:"no-store"`; mutations go through thin Next route proxies that keep the key server-side.

Signature touches: **`[ID]` copy chips** (long ids never shown raw), the **⌘K command palette**, list **filters**, the **conversation/markdown** IO renderer, and **tabbed** trace + span panels.

---

## 🔑 Key decisions — why

| 🧩 Decision | 💡 Why | ⚖️ Tradeoff |
|---|---|---|
| Trace is the source of truth (not dataset-first) | a dataset row can't express "this exact failure trajectory must not recur" | thinner cold-start |
| Agent semantics as **indexed columns** | level lookups are O(1), not read-time parses | wider, opinionated schema |
| **Blob-first** ingestion | nothing lost on a worker/queue outage | an S3 PUT on the request path |
| Embed the **mechanism**, not the topic | cluster by *how* it broke, so unrelated inputs with the same bug group | two texts per trace |
| Skip UMAP on small/duplicate sets | UMAP *invents* clusters on few near-identical vectors | two clustering regimes |
| Fixtures key by **args + order + error** | replay reproduces the real failure, including errors | a richer bundle |
| **Run-outcome** vs strict `no_error` | error-*handling* fixes can pass while crashes fail | a per-case opt-in |
| Only fail-to-pass is a **hard** gate | cost/latency are noisy in replay → warnings | quality regressions can merge until opted in |
| Threads list status = **structural** failures | the strict LLM judge nitpicks good answers → keep the list honest | list vs detail show slightly different status |
| **Evaluators as data** (in progress) | the user owns what counts as a failure | runtime/UI rewiring |
| Hand-rolled design system | distinctive look, tiny bundle | no a11y primitives for free |

---

## 🐛 War stories (bugs we hunted)

- 🌀 **UMAP invented clusters.** The first embedding run merged distinct failure modes and hallucinated "timeout." Root cause: UMAP scatters near-duplicate vectors and HDBSCAN finds phantom structure. Fix: cosine-distance clustering below a size threshold.
- 🧲 **Domain drowned out mechanism.** Embeddings were ~80% shared "weather" text, so the failure mode was noise. Fix: a terse mechanism-focused embedding signature.
- 🧑‍⚖️ **The judge graded the wrong thing.** A hallucination scored a fragile 0.5 — because the judge was grading the *tool's* raw payload, not the agent's answer. Fix: grade the real answer + feed tool results as grounding → it nails it at 0.0.
- 🔒 **Hermetic replay was unsound for errors.** Name-keyed, outputs-only fixtures replayed an errored tool *clean* → a spurious PASS. Fix: ordered, args-keyed, error-bearing fixtures that raise `ToolError`.
- 🩺 **The strict judge made everything look broken.** It failed perfectly fine support answers — which is exactly why the thread list shows *structural* status and why **user-defined evaluators** matter.
- 🩹 **Empty trends charts.** Bars had % heights with no definite-height parent → they collapsed to zero. Fixed the bar component.

---

## ⚠️ Honest limitations

- 🔌 **Replay bridge: tools AND the two big providers' LLM calls are hermetic; other providers fall back to live.** `@observe(as_type="tool")`-decorated tools are **transparently hermetic**: under `with tracely.fixtures(bundle):` the decorator serves the recorded output (or raises `ToolError`) instead of running — no `call_tool` rewrite. On the LLM side, `fixtures()` class-patches **OpenAI `chat.completions`** and **Anthropic `messages`**, so `instrument="auto"` and drop-in (`wrap_openai`/`wrap_anthropic`) agents are served recorded completions and never hit the network. Gemini/Mistral/Bedrock direct calls aren't patched yet — use `call_llm` there for hermetic replay.
- ⏱️ **Latency/cost ≈ 0 in hermetic replay** — the soft delta gates are most meaningful for live cases / instrumented token usage; in pure replay the comparison is informational.
- 📊 **Version attribution scaffolding is unsurfaced** — `agent_versions` is content-hashed and `EvaluationCase.agent_version_first_failed` is set at promote, but no UI shows "v12 introduced this regression". Cheap follow-up, real moat.
- 🛟 **Backups need an operator action** — Postgres + ClickHouse snapshots are a one-click toggle in your provider's UI (see [`DEPLOY.md`](DEPLOY.md)); nothing in Tracely takes them for you.

---

## 🚀 What's next

The strategic plan ("honest gate, visible moat, trustworthy evals") shipped — gate honesty + judge-in-gate + calibration are live. The remaining moves:

**Near-term**
1. **Extend the replay bridge to the remaining providers** — OpenAI + Anthropic (auto-instrument *and* drop-in) already serve recorded completions in replay; add Gemini/Mistral/Bedrock patching so every drop-in agent is hermetic end-to-end.
2. **Surface agent-version attribution** — the scaffolding exists; show "v12 (prompt edit, git a1b2c3) introduced cluster #4" on case + cluster pages.
3. **SDK auth-aware ingest in prod** — first-party Clerk org → ingest key bootstrap so SaaS users don't have to provision keys by hand.

**The bigger vision** *(designed in the dossier, partially built 📐)* — content-addressed `AgentVersion` *gating* (the table exists; the gate doesn't pin to a hash yet), full 7-signal detection + RCA + auto-test-gen, the canary-as-GateRun loop, multi-agent edges + impact analysis, a zero-config GitHub App, and codebase-aware fixes.

> 🧱 **The bet:** the *left* half (ingest → store) is ~70% borrowed from Langfuse's proven substrate; the *right* half (promote → replay → gate, judge-in-gate, calibration) is net-new on top. Build only the trace-native CI layer nobody else has.

---

## 🏃 Run it

```bash
# Whole stack (backend → :8000, frontend → :3001). The `migrate` one-shot applies CH + Alembic
# migrations and seeds the default project + (in dev only) `tracely_dev_key`.
docker compose up -d

# Populate the WHOLE product in one shot — conversations, failure clusters, regression cases.
docker compose --profile demo up -d --build --wait
# …or, if the stack is already running:  make demo  /  docker compose exec backend python scripts/seed_demo.py

# UI → localhost:3001
#   Traces      → conversation threads (multi-turn) with C/M/S rolling summary columns
#   Clusters    → "Analyze failures" → semantic Issues (needs OPENROUTER_API_KEY in .env)
#   Cases       → Promote a failure → a fail-to-pass regression test
#   Judge calibration → ✓agree/✗disagree with judge verdicts before they block CI
#   Gates       → make gate / make replay → hermetic re-run + GitHub PR check

make gate     # gate pre-emitted ci traces by digest
make replay   # re-run the agent (hermetic) + gate (the turnkey path)
```

> 🔑 `OPENROUTER_API_KEY` (judge, failure-intel agents, rolling summary, meta-analysis) + `OPENAI_API_KEY` (embeddings only) live in `.env` (gitignored). Without them the core pipeline runs 100% local & free.

> 🚀 **Deploying to prod?** See [**`DEPLOY.md`**](DEPLOY.md) — required env vars, the refuse-to-boot guards (no `AUTH_MODE=dev` in prod, no seeded `tracely_dev_key`), the worker pool (`CELERY_POOL=prefork`), backups, and post-deploy verification.

---

## 🗂️ Codebase map

```
Tracely/
├── backend/tracely/                    # the shared brain (one package; API + worker share it)
│   ├── api/
│   │   ├── main.py                     # FastAPI app + lifespan (CORS, Sentry, prod guards)
│   │   ├── routers/                    # otlp, traces, sessions, cases, clusters, gate, evaluators,
│   │   │                               #   evaluations, meta_analysis, calibration, analytics, health, auth
│   │   └── auth.py                     # get_principal / get_project_id deps
│   ├── auth/                           # AUTH_MODE=dev|local|clerk — Principal, JWTs, JWKS, provisioning
│   ├── domain/                         # pure logic, no I/O — testable in isolation
│   │   ├── evaluation/                 #   evaluators, verdict (advisory policy), calibration, rolling_summary
│   │   ├── regression/                 #   contract (assertions + judge-in-gate), fixtures, match modes
│   │   ├── failure_intelligence/       #   signatures, clustering, mechanism embedding
│   │   ├── analysis/                   #   meta-analysis statistics (Spearman, z-score, NumPy-deterministic)
│   │   └── traces/                     #   span shaping, metadata, root resolution
│   ├── services/                       # the orchestration layer (sync; uses domain + infrastructure)
│   │   ├── ingestion_service.py        #   OTLP → S3 → enqueue
│   │   ├── evaluation_service.py       #   auto-eval, sampling, targeting
│   │   ├── regression_service.py       #   promote, version_first_failed (scaffolding)
│   │   ├── gate_service.py             #   NO_COVERAGE + soft deltas + judge-in-gate
│   │   ├── rolling_summary_service.py  #   per-span accumulating summary
│   │   ├── meta_analysis_service.py    #   "Analyze" run + persist
│   │   └── seeding_service.py          #   default project; dev-key seeding SKIPPED in prod
│   ├── infrastructure/
│   │   ├── clickhouse/                 #   async_reader, trace_reader (sync), score_writer, schema, migrations
│   │   ├── db/                         #   engine, models, repositories
│   │   ├── llm/                        #   provider (LangChain create_agent on OpenRouter), judge agents
│   │   └── queue/celery_app.py         #   broker config (visibility 3h, time limits)
│   ├── otel/                           # OTLP → first-class columns (multi-vendor lift)
│   ├── workers/tasks.py                # Celery tasks (ingestion + evaluation + cluster rebuild)
│   ├── config.py                       # pydantic settings + prod refuse-to-boot
│   └── log_config.py                   # structlog: JSON in prod, console in dev
├── migrations/versions/                # Alembic — 0001 baseline … 0013 score_annotations
├── workers/tracely_workers/            # thin Celery runtime (entrypoint only)
├── sdk/tracely_sdk/                    # OpenTelemetry SDK + `tracely` CLI (`simulate` / `gate` / `replay`)
│   ├── __init__.py                     #   init/agent/observe/trace/call_tool/call_llm/fixtures
│   └── examples/                       #   25 examples: auto_*/dropin_* per provider + harness + agent SDK
├── frontend/app/                       # Next.js 15 / React 19 — server components, no UI libs
│   ├── (app)/                          #   authed dashboard shell (Sidebar + error.tsx + loading.tsx)
│   │   ├── traces · clusters · cases · gates · trends · calibration · settings · sessions
│   ├── (auth)/                         #   login/register (local mode) + Clerk sign-in/up
│   ├── api/                            #   BFF proxies (forward server-side with auth)
│   └── components/                     #   TraceTable, TracesExplorer, IO, CalibrationView, MetaAnalysisPanel, …
│       └── trace-table/format.{ts,test.ts}  # extracted pure helpers + 37 unit tests
├── scripts/                            # seed_demo.py, run_all_examples.sh, …
├── .github/
│   ├── workflows/ci.yml                # real CI: ruff + pytest + frontend test + build + docker images + audit
│   ├── actions/tracely-gate/           # composite Action for the gate
│   └── dependabot.yml                  # uv / npm / docker × 2 / github-actions
├── design/part2-tracely/               # design dossier (00-canonical is authoritative)
├── DEPLOY.md  ·  DEMO.md  ·  TRACELY_REVIEW.md
└── docker-compose.yml · Makefile
```

---

> 🛰️ **In one line:** Tracely turns your AI agents' worst production moments into tests that guard every future pull request — observed as conversation threads, auto-detected (and **calibrated against humans**), grouped into Issues, frozen with one click, and replayed for free (and faithfully) on every PR with a **judge in the gate**.
>
> *The full closed loop ships — honest gate, visible moat, trustworthy evals. The next frontier is bridging auto-instrument → hermetic replay and surfacing AgentVersion attribution. 🟢*
