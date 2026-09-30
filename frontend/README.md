# `frontend/` — the Tracely web app

Next.js (App Router) UI for the whole product spine — **Observe → Triage → Test → Ship → Insights**. TypeScript, Tailwind CSS, and a deliberately tiny dependency set (only `clsx` on top of `next`/`react`).

- **Next.js 15** (App Router, RSC) · **React 19** · **Tailwind CSS 3.4** (config in [`tailwind.config.ts`](tailwind.config.ts)) · **clsx**.
- No component library, no data-fetching library, no chart library — charts are hand-rolled (`Bars.tsx`), tables are real `<table>`s, state is plain React hooks. This keeps the bundle small and the rendering legible.

```bash
# needs the backend running (see ../README.md). Then:
pnpm install        # or npm install
pnpm dev            # http://localhost:3001   (in Docker it's :3001 too)
```
Two env vars (server-side only): `TRACELY_API` (default `http://localhost:8000`) and `TRACELY_KEY` (default `tracely_dev_key`).

---

## How data flows (the one pattern to know)

There are **two** ways the UI talks to the backend, and which one you use depends on whether the component is a Server or Client Component:

- **Server Components (pages)** call [`app/lib/api.ts`](app/lib/api.ts) **directly** — these run on the server, attach `Authorization: Bearer ${TRACELY_KEY}`, and fetch the backend with `cache: "no-store"`. The key never reaches the browser.
- **Client Components** (the interactive table, the ⌘K palette, action buttons) fetch **Next route handlers under [`app/api/`](app/api/)** instead. Each handler is a thin proxy that re-issues the request to `TRACELY_API` with the Bearer key + `no-store`. This keeps the key + API base server-side and gives the browser clean typed JSON.

> Rule of thumb: a page renders with `lib/api.ts`; anything that fetches *after* a click (lazy expand, search, promote, run gate, SSE eval stream) goes through an `app/api/*` proxy.

---

## App shell

| File | Role |
|---|---|
| `app/layout.tsx` | Root layout: `_providers` (auth context) + `Sidebar` + content (`Topbar` + `<main>`) + `CommandPalette`. Loads display/sans/mono fonts. `<main>` is capped at `max-w-[1240px]` (the trace table can break out of this — see Enlarge). |
| `components/Sidebar.tsx` | Left nav (244px), grouped by the spine: **Observe** (Dashboard, Traces, Trends) · **Triage** (Failure clusters) · **Test** (Regression cases) · **Ship** (CI gates) · **Configure** (Settings) · **Learn** (Documentation, external). Footer shows the project + `prod` env. |
| `components/DocLink.tsx` | The **Docs ↗** pill next to every page/panel title — deep-links into the docs site's product guide (`DOCS_URL` from `lib/site.ts` + a path such as `/product/trends#cross-metric-analysis`). Add one whenever a new screen or non-obvious panel ships, and a matching section under `docs/pages/product/`. |
| `components/Topbar.tsx` | Breadcrumbs + the ⌘K trigger + the onboarding-quest launcher + the theme toggle. |
| `components/CommandPalette.tsx` | ⌘K/Ctrl-K global search → `/api/search`; result types trace / issue / case / gate with keyboard nav. |
| `components/AccountMenu.tsx` | User avatar menu (top-right) — profile, sign out, links to settings. Renders only in `local`/`clerk` auth modes. |
| `app/_providers/` | Auth context provider + Clerk dynamic-import wrapper (auth mode is resolved server-side and passed down as a prop). |
| `app/globals.css` + `tailwind.config.ts` | Theme tokens — `ink` (surfaces), `line` (borders), `fg`/`fg-muted`/`fg-faint` (text), `signal` (cyan accent), `ok/fail/warn/info`, `hilite` (the white-on-dark / ink-on-light tint), span-type colors `t_agent/t_llm/t_tool/…` and `syn-*` (JSON highlighting). Every one is a CSS variable, so the palette has **two themes** (see below). Utilities: `.card`, `.hairline`, `.reveal` (staggered fade-up), `.bg-grid`. |

## Route groups

```
app/
  (marketing)/      # public landing page at "/" — bare layout, no sidebar, no auth
  (auth)/           # sign-in, sign-up, register, login, accept-invite + layout
  (app)/            # authenticated app shell
    dashboard/      # the dashboard — "/" belongs to marketing now
    settings/
      api-keys/     # API key management
      alerts/       # workspace alerts: list, /new, /[monitorId] — the flow builder
      team/         # member list + InviteManager
      account/      # account settings + change password (local mode)
```

## Pages (`app/**/page.tsx`)

All are **Server Components** unless noted; each lists the `lib/api.ts` calls it makes.

| Route | Fetches | Renders |
|---|---|---|
| `/` | — | **Public marketing landing page** (`(marketing)/`) — no auth, no app shell. Client component animated with GSAP; all motion behind `gsap.matchMedia("(prefers-reduced-motion: no-preference)")`. |
| `/dashboard` | `getStats`, `getTraces`, `getCases`, `getEvaluators`, `getGates`, `getMe` | Dashboard — the **Activation** checklist (trace → grade → failure → case → gate; every step derived from real counts, the card disappears once the loop has been closed once), 4 stat cards + recent traces & cases. |
| `/traces` | `getSessions` | `TracesExplorer` (filter + search + date range) wrapping the hierarchical **TraceTable** in list mode. |
| `/traces/[traceId]` | `getTrace` | Single trace header (spans/latency/**usage totals**, `PromoteButton` if failing) + `SingleTraceView` (Table / Timeline tabs + an Agents drawer). |
| `/sessions/[threadId]` | `loadConversation` (`lib/conversation.ts`) | A conversation, pre-expanded: builds a `ConvNode` with all turns + spans and renders **TraceTable** in detail mode. `?view=timeline` opens on the waterfall. |
| `/sessions/[threadId]/replay` | `loadConversation` + client → `/api/session-replay` | Conversation replay — the thread acted out on one scrubable clock: a lane per agent (sub-agents nested), step log following the playhead, containers drawn as hollow envelopes. |
| `/sessions/[threadId]/fleet` | `loadConversation` + client → `/api/session-replay` | Conv fleet — the same script as a pixel office: a desk per agent, skills at the library, tools at the wall, delegations walking over with speech bubbles, thought clouds while thinking; click a character for its personnel file (declared agent-definition + observed models/tools), or a book on the library / a tool on the wall for its card (declared description, who used it, last result). |

**Conversation chrome.** Table, Timeline, Replay and Fleet are four lenses on one thread, so all four pages call `loadConversation` and render the same `ConversationHeader` + `ConversationTabs` (`components/ConversationChrome.tsx`): identical header, identical tab strip, only the body changes. Table/Timeline stay client-side tabs; Replay/Fleet are routes, linked from the same strip. The evals verdict pill in that strip is the link to `/evals`.
| `/sessions/[threadId]/evals` | `getChainProgress` + `getSession`/`getTrace` per eval level | How Tracely graded this conversation: the sequential-chain status card (per column: turns chained, up-to-date/behind, last payload) above one tab per eval level (`eval:<thread>:step\|msg\|conv` recordings). |
| `/clusters` | `getClusters` | Failure-cluster table + `RebuildButton` ("Analyze failures"). |
| `/clusters/[clusterId]` | `getCluster` | Issue detail — histogram, description, proposed fix, suggested evaluator (`CodeBlock`), member traces, `ClusterActions`. |
| `/cases` | `getCases` | Regression cases — title, status, fail→pass contract, last verdict, source trace. |
| `/traces` | `getSessions` | Conversation list. `TracesExplorer`'s **Evals** filter chip re-queries with `evals=1` and shows ONLY Tracely's own runs (the judge's prompt and reply, the attacker's move, the POST to your endpoint), tagged `EVAL`/`SIM`; they open like any trace. It is the one filter that hits the server — the rest refine the loaded rows. |
| `/scenarios` | `getAgents`, `getScenarios` | Multi-turn conversations driven against the agent's HTTP endpoint — `ScenariosManager` (agent picker ranked by scenario count, `EndpointPanel`, list, inline create/edit via `ScenarioForm` + `TurnEditor`). |
| `/cases/[caseId]` | `getCase` | Case detail — assertions, reference trajectory, `ReplayControls` + replay history. |
| `/gates` | `getGates`, `getAgents`, `getCases` | Gate runs — result, agent/env/ref, passed/failed/skipped, plus `RunGateButton` (agent picker, ranked by promoted-case count — no hardcoded slug). |
| `/gates/[gateId]` | `getGate` | Gate detail — status banner, soft warnings, per-case verdicts. |
| `/trends` | `getTrends` | Insights — stat cards + `Bars` charts (daily traces/failures, gate pass/fail) + `MetaAnalysisPanel` (per-agent cross-metric analysis). |
| `/settings/api-keys` | — | API key management (create/revoke ingest keys). |
| `/settings/alerts` | `getMonitors` | Alert rules — a use-case gallery that opens a pre-drawn flow, plus each rule's trigger, its flow as a strip, arm toggle and last-fired line (`AlertsList`). |
| `/settings/alerts/new` | `getAgents`, `getEvaluators` | A new rule, optionally seeded from `?recipe=<i>` (ids are minted here, so a recipe is data, not a half-saved rule). |
| `/settings/alerts/[monitorId]` | `getMonitor`, `getAgents`, `getEvaluators` | The flow builder: React Flow canvas + docked inspector, the assistant panel, a real test run and the last ten runs (`RuleEditor`). |
| `/settings/team` | — | Team members list + `InviteManager` (send/revoke invitations). |
| `/settings/data` | `getStats` | What the project holds (traces/spans/agents/cases) + the `WipeDataPanel` danger zone (delete all project data, typed confirmation). |
| `/settings/account` | — | Account settings + `ChangePasswordForm` (local auth mode). |

## Data layer

- **`app/lib/api.ts`** — server-side fetchers + all shared types. One function per backend endpoint (`getSessions`, `getSession`, `getTrace`, `getClusters`, `getCases`, `getGates`, `getTrends`, `getStats`, …) plus the type model the whole UI shares: `SpanOut`, `EvalScore`, `Thread`/`ThreadTurn`/`FullTurn`/`ConvNode` (the conversation→turn→span tree), `EvalCase`, `FailureCluster`, `GateRun`, `Stats`, `Trends`.
- **`app/lib/alerts.ts`** — the trigger half: `TRIGGERS` (the six condition types, their family and which fields each shows), `RECIPES` (the gallery, each with a starter flow), `triggerSummary`, and `toBody`/`fromMonitor`/`draftProblem`. `toBody` sends only the fields the chosen trigger uses — a leftover `threshold` on an event condition would read as a filter nobody set. Unit-tested in `alerts.test.ts`.
- **`app/lib/ruleFlow.ts`** — the flow half, and **the file that has to match the backend exactly**: dedupe edges → BFS reachability from `__rule_trigger__` → Kahn with sorted-id tie-breaks, plus ancestors (positional `steps[i]`), `flowToStepDrafts`, `buildFlowFromRule`, the step palette and the `{{ token }}` splitter. `domain/alerting/flow.py` implements the same three stages; if they drift, a rule runs differently than it looked on screen. Tested in `ruleFlow.test.ts` against the same cases as `backend/tests/test_alert_flow.py`.
- **`app/lib/evaluators.ts`** — evaluator CRUD helpers + types (`EvaluatorRow`, `EvaluatorTemplate`, `EvaluatorLevel`), the models/cost lookups, and `resolvePromptPreview`. Wraps the `/api/evaluators/*` proxies for client-side fetches.
- **`app/lib/templateVariables.ts`** — the client mirror of the backend `@VARIABLE` catalog (names, descriptions, applicable levels, nested props) + the `@VARIABLE` regex; drives the advanced editor's highlighting + autocomplete.
- **`app/lib/usage.ts`** — pure token/cost derivation, shared by the table **and** the detail-page headers so they compute identically. `spanUsage`/`turnUsage`/`convUsage` aggregate input/output/thinking tokens; `rateFor` prices them from a per-model rate table; `usageSummary`/`fmtUsd` format. `total_tokens` = input + output (matches the backend total); thinking tokens are surfaced separately.
- **`app/api/*/route.ts`** — the client→backend proxies: `session` (lazy-load a conversation's turns), `trace` (lazy-load a turn's spans), `search` (⌘K), `evaluators/` (CRUD + generate + models/cost + `resolve` preview), `evaluations/run` (SSE run stream), `meta-analyses/` (agents/run/latest), `sessions/[id]/` (rolling-summary + agents), `assistant` + `assistant/chats/` + `assistant/upload` + `assistant/files/` (the chat widget: one turn as a piped SSE stream, its history, and attachments in and out), `onboarding` (quest counts), `auth/` (me/login/register/logout/change-password/invite/accept-invite/projects/switch), plus action proxies (`promote`, `cluster`/`cluster-rebuild`, `gate`, `replay`). Each forwards to `TRACELY_API` with the Bearer key + `no-store`.

## Components

**`TraceTable.tsx`** is the centerpiece — a real `<table>` rendering the **Conversation → Message → Step** tree (modeled on a TurnWise-style spreadsheet):
- **Column groups** with level badges and subtle group dividers: **C** (conversation: title, time, duration, summary, **metadata**, usage), **M** (message/turn: role, #, time, duration, content, usage), **S** (step/span: #, type, time, duration, agent, model, name, input, output, usage). Depth-coloured left borders (C=blue, M=green, S=purple).
- **Evaluator columns** — each enabled evaluator appears as a dynamically-loaded column. The header is a button that opens `AddColumnModal`; cells render a score pill (value + verdict badge) that opens a floating `FloatingPanel` (via `JsonView.tsx` Pill/FloatingPanel) with the full score detail. PASS/FAIL/numeric/boolean/text/JSON outputs each render appropriately. Clicking the pill does not propagate to row navigation (portal event isolation).
- **Rich cells:** any message object (`{role, content}`) — including assistant **completions** — renders as a compact role pill that opens a floating bubble panel; raw structured data with no `role` → a `{ }`/`[ ]` JSON pill with a syntax-highlighted panel; multimodal message content → text + clickable image/file chips; per-level **usage** → a Σ pill with a Tokens/Cost breakdown; `THINKING` is a first-class span type. Floating panels use `createPortal` to escape the table's overflow.
- **Two modes:** *list* (seeded with conversation summaries; turns + spans **lazy-load** on expand via the `/api/session` and `/api/trace` proxies) and *detail* (the whole tree pre-seeded, everything open).
- **Controls:** Expand/Collapse All (cascades to the step level), a Columns visibility menu, an Enlarge (full-width breakout) toggle, and the **+ Add Column** button to manage evaluator columns — all **persisted to `localStorage`**.
- **`trace-table/` holds the parts that aren't about tables:** `content.tsx` (how a payload becomes readable — chat transcripts, multimodal blocks, tool calls, state writes, and the badges that label them; it knows PROVIDER SHAPES, touches none of the table's state, and is tested against real wire payloads in `content.test.tsx`), `format.ts` (pure text/number helpers), `useConversationTree.ts` (what is open and what has been fetched — the lazy Conversation→Turn→Step tree; `undefined` = never asked, `"loading"` = in flight, `[]` = asked and failed, so a broken row settles instead of retrying forever), `columns.ts`, `icons.tsx`.

| Component | Role |
|---|---|
| `TracesExplorer.tsx` | `/traces` filter (All/Failing/Multi-turn) + search + `DateRangePicker`, wrapping `TraceTable` (list mode). |
| `SingleTraceView.tsx` | One trace as tabs: Table (`TraceTable` detail) / Timeline (`Waterfall`), plus an Agents drawer button (evaluations are inline columns now — no Evaluations tab). |
| `SessionView.tsx` | Conversation-level view wrapping `TraceTable` (detail mode) + the Agents drawer (`AgentsSidePanel`). |
| `Waterfall.tsx` | Gantt-style span timeline (bars by type, depth-indented, I/O on expand). |
| `IO.tsx` | Smart input/output renderer (chat arrays → bubbles, objects → JSON, else text). |
| `JsonView.tsx` | Shared JSON rendering primitives: `HighlightedJson`, `prettyJson`, `Pill`, `FloatingPanel`, `IconBox`, `JsonPill`, `ExpandableText`, `Plain`. Used by the table, timeline, and attributes panel. |
| `AddColumnModal.tsx` | Multi-step modal for adding/editing evaluator columns: pick type (browse catalog / manual / AI-generate) → pick granularity level → configure (basic prompt **or** advanced `@VARIABLE` editor + live preview, output type, model, threshold, output schema, targeting/sampling/advisory). |
| `AdvancedPromptEditor.tsx` | The advanced judge prompt editor — a transparent `<textarea>` over a synced highlight overlay (so `@VARIABLE` tokens glow), with `@`/`.`-triggered autocomplete at the caret. |
| `VariableAutocomplete.tsx` | The presentational autocomplete dropdown for the advanced editor (editor owns the candidate list + insertion). |
| `PromptPreview.tsx` | Live preview — resolves the advanced prompt against a real conversation/turn/step (`/api/evaluators/resolve`) with used (green) / missing (amber) variable badges. |
| `OutputSchemaBuilder.tsx` | Drag-and-drop JSON schema builder for LLM-judge `json` output type — fields with names, types, descriptions, and enum constraints. |
| `SuggestedEvaluatorCard.tsx` | The cluster-detail "Suggested evaluator" panel — opens the backend's creatable draft prefilled in `AddColumnModal` to review/edit/save. |
| `MetaAnalysisPanel.tsx` | The Trends-page meta-analysis ("Analyze") — pick an agent, run, render patterns/correlations/outliers/recommendations, export as Markdown. |
| `AgentsSidePanel.tsx` | Right-side drawer (portal) listing a conversation's agents exactly as a judge reads `@AGENTS`: the SDK catalog merged with the tools the spans show offered/called, each tool marked by source (`agentsPanel.ts` holds the pure helpers). |
| `Assistant.tsx` | The in-app chat widget (bottom-right launcher → panel), mounted once in the `(app)` layout so it survives navigation and knows the current route. An **agent**: it reads this workspace's traces to answer and can create evaluators, scenarios and regression cases, so a turn takes tens of seconds and **streams** (`app/lib/assistant.ts` decodes the SSE) — the panel names the tool it is running, then types the answer out. Two views — the current conversation, and the history you can go back into — over `assistant_chats` in Postgres, so coming back tomorrow reopens where you left off (only *which* chat was last open is local). Attachments upload on pick, drop or paste: images render inline in the bubble, other files as download chips. Replies render through `Markdown.tsx`. The model runs on Tracely's own LLM key, not the workspace's, so it answers in a brand-new workspace too; its tools run as you. A turn can be **cancelled** (the send button becomes Stop, and closing the panel aborts too) — a tool loop left running costs real money nobody is reading. |
| `OnboardingQuest.tsx` | The gamified onboarding checklist — a progress-ring launcher in the `Topbar` opening a dropdown (panel portalled to `<body>`: the topbar is a `sticky z-20` stacking context). Steps derive from `/api/onboarding` counts + visited routes; daily challenges, score and streak live in `localStorage`. |
| `Markdown.tsx` | Minimal dependency-free Markdown renderer (used by previews + the meta-analysis panel). |
| `ChangePasswordForm.tsx` | Account-settings change-password form (local auth mode). |
| `alerts/AlertsList.tsx` | The `/settings/alerts` body: the use-case gallery (a click opens a pre-drawn flow) and the rule list — trigger, scope, the flow as a strip of step chips, arm toggle, last fired. |
| `alerts/RuleEditor.tsx` | The builder page body: name/description/arm, the canvas, the assistant panel, Save, a real test run and the run history. Owns the trigger draft; the canvas owns the graph and hands over a save payload on demand, so there is no second copy of the flow to drift. |
| `alerts/RuleFlowCanvas.tsx` · `useRuleFlow.ts` · `nodes.tsx` | The canvas: `ReactFlowProvider` + the pane, all state and handlers in the hook (the components are markup), and the two node types + the step picker. The trigger node is undeletable by filtering the *change*, nothing may connect INTO it, and clicking an edge blurs the inspector first so Backspace deletes the edge rather than a character. |
| `alerts/InspectorPanel.tsx` · `StepConfigForm.tsx` · `TriggerConfigForm.tsx` | The docked inspector: input chips │ config form │ declared outputs. Selecting the When node swaps the middle column for the trigger's own form, so configuring the trigger is the same gesture as configuring a step. |
| `alerts/VariableFields.tsx` | Template fields: a draggable variable chip, and the input/textarea that accept one. A native field with transparent text over an `aria-hidden` mirror that highlights `{{ tokens }}` — no editor library, and Backspace next to a token removes the whole token. |
| `alerts/AssistantPanel.tsx` | "Describe the alert you want" → a drafted flow pushed onto the live canvas via `replaceFlow`. It hands the page a draft and edits nothing itself. |
| `alerts/ExecutionCard.tsx` | One run, per step: what it **sent** (every field after templating) and what it **returned**. The rendered-config half is why a run explains itself without re-running. |
| `InviteManager.tsx` | Team invite flow (send invitation by email, list pending invitations, revoke). |
| `DateRangePicker.tsx` | Date range filter for the traces explorer. |
| `ui.tsx` | `Badge`, `verdictVariant`/`statusVariant`, `TypeChip` (span-type chip), `StatCard`. |
| `icons.tsx` | Inline stroke SVG icon set. |
| `Bars.tsx` | Hand-rolled stacked bar charts for `/trends`. |
| `CopyId.tsx` · `TimeAgo.tsx` · `CodeBlock.tsx` · `RowLink.tsx` | Copy-to-clipboard id chip · relative time (SSR-safe) · syntax-highlighted code w/ copy · clickable row wrapper. |
| `ScenariosManager.tsx` | The `/scenarios` page body: agent picker, endpoint panel, conversation list (row click opens the inline editor), and one `ScenarioForm` used for BOTH create and edit — a second form is how the two drift. |
| `TurnEditor.tsx` | Multi-turn conversation editor: one row per turn (a turn is a message, not a line), add/remove/reorder, plus each turn's optional `expect` + `tools` expectations behind a toggle. `idPrefix` namespaces field ids so a create form and an editor can be open at once. |
| `EndpointPanel.tsx` | Where Tracely calls the agent. The token is write-only — encrypted server-side, and the GET only reports `has_token`, so it is never rendered back into the browser. |
| `Toggle.tsx` | Themed on/off switch (`peer sr-only` + styled track, same idiom as `SelectBox`). Replaces `accent-signal` checkboxes, which render as the OS control and read as foreign. |
| `GateAutoRefresh.tsx` | Mounted only while a gate has no `finished_at`: re-fetches until the async simulated run settles. |
| `SaveAsScenarioButton.tsx` | On a session page — turns that production conversation into a scenario. |
| `DeleteCaseButton.tsx` | Delete one regression case from its detail page (`confirm()` → `DELETE /api/cases/{id}` → back to `/cases`). |
| `WipeDataPanel.tsx` | Settings → Data danger zone: type `DELETE` to arm, then `DELETE /api/project/data`; renders the per-table counts that came back. |
| `PromoteButton` · `RebuildButton` · `RunGateButton` · `ReplayControls` · `ClusterActions` | The write actions (promote a trace, rebuild clusters, run a gate, replay a case, ignore/promote a cluster) — each POSTs an `app/api/*` proxy. |

## Key decisions (and why)

1. **Tiny dependency surface.** Only `clsx` beyond Next/React — no UI kit, table lib, or chart lib. The UI stays fast, legible, and fully in our control (the trace table needed bespoke rendering anyway).
2. **Server fetch for pages, proxy for clicks.** Pages fetch the backend directly (key stays server-side, no client waterfall); interactive fetches go through `app/api/*` so the key/API base are never in the browser and caching is forced off.
3. **Usage math lives in one pure module.** `lib/usage.ts` is shared by the client table and the server-rendered headers, so a step, a message, a conversation, and a page header always agree on tokens + derived cost.
4. **The trace table is a real `<table>`, lazy and rich.** Hierarchical conv→message→step with per-level columns and inline pills; list mode lazy-loads so `/traces` stays cheap, detail mode pre-seeds so a single conversation renders fully.
5. **Evaluators as dynamic columns.** Each enabled evaluator is a column in the same table — no separate Evaluations tab to navigate to. Adding a column is a guided modal flow; the column's scores stream in live via SSE.
6. **Portal event isolation for floating panels.** `FloatingPanel` renders into `document.body` via `createPortal`. The backdrop click calls `e.stopPropagation()` to break React's synthetic event bubbling and prevent row-navigation events from firing.
7. **Cost is derived in-app.** The backend doesn't trace cost, so price comes from a per-model rate table in `usage.ts`; one open question (see the PRD) is whether to compute `cost_details` at ingest instead so it's authoritative everywhere.
8. **Theme as tokens.** Semantic color tokens (`ink/line/fg/signal/ok/fail/...` + span-type colors) keep the UI consistent and make per-type/per-verdict styling declarative — and because every token is a CSS variable, one attribute repaints the whole app.

## Dark and light

`tailwind.config.ts` names no colour. Each one is `rgb(var(--c-x) / <alpha-value>)`, and the two palettes are two blocks in `app/globals.css`: `:root, [data-theme="dark"]` (the default) and `[data-theme="light"]`. Rules:

- **Never hardcode a colour in a component.** `bg-white/[0.04]` is a dark-theme assumption — use `bg-hilite/[0.04]`, which is white on dark and ink on light. Raw Tailwind palette classes (`text-cyan-300`) are the same mistake; the JSON viewer uses `text-syn-str` and friends.
- **Both blocks or neither.** A token defined in one theme and not the other silently inherits the other's value (white-on-white). `app/globals.theme.test.ts` fails the build for it.
- **The switch is `<html data-theme>`**, written before first paint by the inline script in `app/layout.tsx` and flipped by `components/ThemeToggle.tsx` (localStorage key `tracely-theme`, default dark). A React effect would flash.
- **A subtree can pin a theme** by setting `data-theme` on a wrapper — that is why the dark block is also keyed on `[data-theme="dark"]`. The marketing page (`app/(marketing)/layout.tsx`) and the Fleet office diorama do exactly that: they are art directed dark and stay dark whatever the app is set to.
- Light values are picked for **WCAG AA on their own surface** (body text ≥ 7:1, muted text and accents ≥ 4.5:1 on the canvas, the card, and their own 15% tint) — keep that when adding one.
- `app/`, `middleware.ts` and `tailwind.config.ts` are the only paths volume-mounted into the Docker frontend, which is exactly what a palette change touches — edits show up on :3001 without a rebuild.
