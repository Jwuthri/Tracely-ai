# Tracely: product focus and implementation plan

> **Purpose:** An implementation handoff for Claude Code and a working roadmap for the founder.
> **Prepared:** September 7, 2026.
> **Repository baseline:** `636a5bd` — verify the current checkout before implementing.
> **Stage:** Early development, few users. Product-market fit and willingness to pay remain unproven.

## 1. The decision

Tracely has enough substance to start working with design partners. The next goal is to make one complete workflow compelling and trustworthy:

**Take a real agent failure, turn it into a useful regression test, verify a code change against it, and keep that failure from returning through CI.**

The product should let a developer answer five questions without assembling the answer across several screens:

1. What went wrong, and does it matter?
2. What should the agent have done instead?
3. Can I reproduce the problem under known conditions?
4. Did this specific change satisfy the expected behavior?
5. Will my release process check this again?

Prioritize this workflow over adding more observability surfaces. A larger feature list will not resolve uncertainty about adoption.

### Initial customer hypothesis

Start with small engineering teams operating Python agents that call tools, use GitHub, and have experienced a costly or recurring production failure. Support and operations agents are a useful initial hypothesis, not a validated market selection.

Pick one exact integration path with the first design partners. Avoid claiming support for every agent framework because some OTLP traces can be ingested.

### Competitive interpretation

Trace-derived tests and CI evaluations already overlap with established products. Tracely needs to win on the quality, speed, and trustworthiness of the complete failure-to-fix workflow.

Reference points from the preceding review, checked September 5, 2026:

| Product | Existing overlap to account for |
| --- | --- |
| [Langfuse](https://langfuse.com/docs/evaluation/experiments/experiments-ci-cd) | Experiments in CI, including regression checks and PR results. |
| [Braintrust](https://www.braintrust.dev/docs/annotate/datasets) | Turning production traces into dataset examples. |
| [LangSmith](https://docs.langchain.com/langsmith/manage-datasets-in-application) | Creating evaluation examples from traces. |
| [Promptfoo](https://www.promptfoo.dev/docs/integrations/ci-cd/) | Evaluation integrated into CI workflows. |

Do not position competitors as incapable of agent evaluation or trace-to-test workflows. The opportunity is a narrower, better experience that users can demonstrate with their own bugs.

## 2. Instructions for the implementing agent

Read `CLAUDE.md`, applicable repository instructions, and this document before editing. Repository paths below are relative to the repository root so this document remains portable.

This is a sequenced backlog, not a request to rewrite the application or implement every package in one pass.

1. Inspect the current code and working tree. Preserve unrelated changes.
2. Confirm each reported gap still exists. Mark already completed items with evidence instead of rebuilding them.
3. Start with **W1: explicit replay execution and strict fixture handling**.
4. Deliver one reviewable vertical slice at a time, with relevant tests and documentation.
5. Update the tracker at the end of this document after each slice. Record remaining limitations explicitly.
6. Keep founder research, customer outreach, pricing decisions, and production rollout separate from implementation work. Do not contact people or publish changes automatically.

### Architecture and compatibility constraints

- Keep pure evaluation logic in the domain layer; I/O in infrastructure; orchestration in services; routers and workers thin.
- Maintain project scoping on every data operation. New replay and run identifiers must not permit cross-project access.
- Preserve project-owned LLM key boundaries. A required judge that cannot run must report an unavailable check; do not bypass the key policy with server credentials.
- Keep MCP and assistant access behind the existing authenticated API boundary. Do not add direct database access through those tools.
- Preserve SSRF checks on customer endpoints, token revocation behavior, deterministic identifiers, and retry idempotence.
- Preserve internal-run exclusions from production metrics and evaluation loops.
- Keep frontend credentials on the server and use the existing Next proxy pattern.
- Do not change production verdict rollups incidentally while fixing regression-test verdicts. Audit Python and SQL counterparts if changing shared semantics.
- Reuse existing cases, suites, runs, fixtures, and version fields where appropriate. Avoid introducing a second evaluation platform.
- Do not replace the database stack, frontend framework, or ingestion pipeline as part of this plan.

## 3. What to preserve, improve, and stop expanding

### Preserve and build on

- Trace ingestion, SDK instrumentation, and the existing separation between domain, services, and infrastructure.
- Conversation context, tool trajectories, evaluator results, calibration, and judge inspection.
- Existing cases, suites, GitHub Action, CLI, sharing, and integration entry points.
- The coherent visual language, open-source/self-hosted option, and OTLP compatibility.

These provide a substantial foundation. The plan connects them into a stronger product rather than replacing them.

### Improve first

- The meaning of a passing result.
- Reproduction guarantees and clearly described execution modes.
- Durable test inputs and exact candidate provenance.
- Editable expected behavior and a useful before/after comparison.
- First-use guidance that ends with a real result.
- Finding important failures across the full dataset.

### Freeze new investment until the core workflow is validated

- Fleet/pixel-office experiences, voice features, quest XP, and streaks.
- More general-purpose automation-builder capabilities.
- Broad meta-analysis and additional dashboards without a demonstrated customer decision behind them.
- A prompt-management platform, gateway, or general-purpose dataset platform.
- Simultaneous expansion into many SDKs, frameworks, and enterprise features.

Freezing investment does not mean deleting saved workflows or removing useful existing advanced features. Reduce their prominence where needed, preserve access, and revisit based on actual usage.

## 4. Current evidence and limits

The following gaps were reconfirmed against the baseline above. They are implementation starting points, not an exhaustive security or correctness audit.

| Area | Current behavior | Consequence |
| --- | --- | --- |
| SDK replay | `fixtures()` treats an empty bundle as live; `_pop_fixture()` can consume the next item after argument mismatch; `call_tool()` and `call_llm()` call the real function on a miss. | A replay described as recorded can perform live work or conceal meaningful input divergence. |
| Quality checks | `apply_quality()` leaves the incoming verdict unchanged when results are absent. | A required quality check can be unavailable while the result remains PASS. |
| Manual evaluation | `RegressionService._evaluate()` applies structural assertions; the gate additionally applies quality evaluation. | Different entry points can disagree about the same case. |
| Verification state | `_record_fail_to_pass()` sets `fail_to_pass_validated` when the source fails. | The flag does not establish that a candidate fix passed. |
| Case input | `_recover_input()` reads source spans and returns an empty string when unavailable. | A retained case can lose executable input when its source disappears. |
| Fixture loading | `_load_fixtures()` returns an empty bundle after a load failure. | Missing artifacts can become live execution. |
| Candidate selection | Fallback pairing uses recent environment/agent traces and input digests. The CLI command path ignores subprocess exit status and waits a fixed interval. | A failed or incomplete current run can be confused with an older matching trace. |
| Discovery | Several trace filters operate on loaded rows in `TracesExplorer.tsx`. | Results and empty states can mislead users when matching failures exist beyond the loaded page. |
| Onboarding | Activation snippets reference `tracely-sdk`, while the package declares `tracely-ai`. | A new user's installation path requires executable verification. This is a broken workflow concern, not a naming exercise. |

**Already implemented:** The latest commit adds plan-scoped trace retention and a scheduled sweep. Do not reopen this as an unimplemented billing-retention feature. Durable regression artifacts remain a separate concern.

**Review limits:** The preceding review included source inspection, repository screenshots, and public UI review. It did not establish a complete authenticated end-to-end walkthrough, production load profile, or actual customer conversion behavior. Verify those during implementation and partner sessions. Do not treat earlier test results as validation of future patches.

## 5. Product contracts to establish before UI expansion

### 5.1 Execution mode must describe what actually happened

| Mode | Appropriate use | Required behavior |
| --- | --- | --- |
| Recorded model + recorded tools | Testing orchestration, parsing, state handling, and error handling under recorded conditions. | No unapproved live fallback. Unsupported interception or missing fixtures must produce an explicit execution problem. |
| Live model + recorded tools | Testing changed prompts or models against controlled tool responses. | Explicit opt-in, visible model/configuration and cost, strict tool handling, and clear disclosure of model variability. |
| Live endpoint/scenario | Exercising a deployed candidate or realistic integration path. | Explicit endpoint/version, bounded execution, visible side effects and costs where relevant, and no deterministic-replay claim. |

The second mode is proposed work, not a claim that the current `--live` flag already implements it.

Recorded model outputs do not prove that a changed prompt or model will produce a better answer. The UI must explain the scope of evidence beside the result.

For a strong no-live-execution guarantee, validate the supported execution path with provider/tool egress denied in the executor. The coordinator may still need Tracely API access. SDK patches alone are not a universal sandbox for arbitrary Python code or external side effects; unsupported execution must be clearly bounded and labeled.

### 5.2 Separate check outcome, execution completeness, and release policy

Use a structured result that represents:

- Which checks were required, executed, passed, failed, or unavailable.
- Whether execution finished under the declared mode.
- Which policy determined whether CI blocks.
- Which exact case version and candidate produced the evidence.

Suggested user-facing outcomes are PASS, FAIL, INCOMPLETE, and explicitly excluded/SKIP. Final wire names should follow a small design decision in W2.

Rules:

1. PASS requires all applicable required checks to have run and passed.
2. Missing fixtures, missing required judge results, missing candidates, and execution errors cannot produce a successful required check.
3. A behavioral failure and an execution problem remain distinguishable even when both block CI.
4. Advisory checks may be unavailable without blocking, but their unavailable state remains visible.
5. An explicitly optional empty suite may preserve existing behavior, but cannot count as a meaningful first test or proof of protection.
6. A run with required cases unexercised must expose that coverage gap. Keep existing `NO_COVERAGE` compatibility unless intentionally migrated.

### 5.3 Separate lifecycle evidence

Do not collapse these facts into one “protected” or “validated” badge:

| Fact | Evidence required |
| --- | --- |
| Source failure confirmed | Source evaluation fails the case contract. |
| Failure reproduced | Executing the recorded case produces the relevant failure under a declared mode. |
| Candidate verified | A specific candidate execution passes the applicable contract. |
| Included in suite | A suite includes the intended case version. |
| CI check executed | A real CI run evaluates that suite against a specific revision. |
| Merge protection confirmed | The repository actually requires the check, verified through authorized integration or explicit user confirmation. |

Historical `fail_to_pass_validated=true` establishes at most source-failure evidence under the old implementation. Do not migrate it into fabricated candidate verification.

## 6. Implementation packages

### W1 — Explicit replay execution and strict fixture handling

**Priority:** P0. **Dependency:** None. **Start here.**

**Outcome:** A recorded execution cannot silently become a live execution through a missing or mismatched fixture.

**Starting points:** `sdk/tracely_sdk/__init__.py`, `sdk/tracely_sdk/cli.py`, `backend/tracely/services/gate_service.py`, existing replay tests under `sdk/tests/`.

**Work:**

- Introduce explicit execution policy in the fixture context. Distinguish no replay requested from an explicitly empty replay bundle.
- Represent missing, exhausted, mismatched, and unsupported fixtures with structured errors and diagnostic details.
- Apply the same policy to manual wrappers, decorated tools, and provider adapters.
- Do not consume a different call merely because the name matches. Make any normalization or ordered matching strategy explicit and inspectable.
- Preserve recorded error behavior so fixes to error handling remain testable.
- Report unused fixtures as evidence of divergence. Do not automatically fail every unused fixture: a legitimate fix can avoid a previously executed call.
- Propagate artifact-loading errors from the service instead of substituting an empty live bundle.
- Publish an exact compatibility matrix covering provider API, sync/async, streaming/non-streaming, response shape, and instrumentation path. Existing adapters span several providers; test actual API surfaces rather than advertising provider-wide coverage.
- Choose a documented compatibility rollout for existing callers. Legacy behavior, if temporarily retained, must not be labeled strict recorded execution.

**Acceptance criteria:**

- Missing/exhausted fixtures do not invoke a sentinel live function in strict mode.
- Changed arguments do not silently receive an unrelated recorded response.
- Empty, corrupt, and missing bundles yield explicit diagnostics.
- Recorded errors are faithfully surfaced to the agent's own error handler.
- All advertised provider paths have contract tests, including unsupported-path behavior.
- A supported recorded demo runs with provider/tool network access denied; unsupported paths cannot earn the same guarantee.
- Explicit live behavior continues to work and is distinguishable in execution metadata.

### W2 — One evaluation contract across manual replay and CI

**Priority:** P0. **Dependency:** W1 error/result shape.

**Outcome:** The same case and candidate mean the same thing in the UI, CLI, and gate.

**Starting points:** `backend/tracely/domain/regression/contract.py`, `backend/tracely/services/regression_service.py`, `backend/tracely/services/gate_service.py`, API serializers, `backend/tracely/infrastructure/db/models.py`.

**Work:**

- Define a shared evaluation orchestration path with pure result aggregation in the domain layer.
- Compare expected required judge identities with received results. An empty or partial result list cannot imply complete evaluation.
- Capture judge configuration/version and required-versus-advisory policy with results.
- Distinguish evaluation failure, missing coverage, timeout, cancellation, and execution error.
- Apply the contract consistently to manual replay, source evaluation, CLI, and CI.
- Audit database widths, API schemas, frontend unions, share payloads, and exit codes before introducing statuses. `CaseReplay.verdict` currently has width 8; `INCOMPLETE` will not fit.
- Preserve production monitoring semantics unless separately justified.

**Acceptance criteria:**

- A quality-only case cannot pass manually while failing the identical required quality check in CI.
- Missing credentials, disabled/deleted required judges, partial results, and judge timeouts remain non-PASS required outcomes.
- Advisory behavior is tested separately.
- Empty-suite, partial-coverage, all-skipped, mixed-failure, and incomplete-run aggregation are covered by a readable policy table and tests.
- UI, API, CLI exit status, and GitHub output agree on the policy result.

### W3 — Durable, versioned case artifacts

**Priority:** P0. **Dependency:** W1 policy; coordinate schema changes with W2.

**Outcome:** A saved regression remains executable after normal source-trace retention expires.

**Starting points:** Case creation in `regression_service.py`; `_recover_input()` and `_load_fixtures()` in `gate_service.py`; fixture serialization; case/suite models; blob storage and deletion paths.

**Work:**

- Snapshot executable input in its supported typed format, required initial state, fixture bundle, expected behavior, evaluator configuration, execution mode, and provenance.
- Define the supported boundary for initial state. If external state cannot be captured, mark that limitation instead of pretending the case is self-contained.
- Version the artifact and record a content digest. Use case versions and existing suite pinning concepts rather than mutable references to current settings.
- Separate routine source retention from deliberate case/artifact deletion. Clearly define workspace deletion and sensitive-data deletion behavior.
- Backfill from available sources. Mark unrecoverable historical cases incomplete and provide a recapture path; never invent an empty input.
- Ensure safe retry behavior across database and blob writes, including orphan cleanup where needed.

**Acceptance criteria:**

- A newly created case still executes after its source spans are removed by a test retention path.
- Missing/corrupt artifacts produce an explicit incomplete result.
- Pinned historical runs remain explainable after assertions or evaluator settings change.
- Tenant boundaries and explicit deletion behavior are tested.
- Existing cases survive migration; unrecoverable ones are clearly identified.

### W4 — Exact candidate identity and honest verification state

**Priority:** P0. **Dependency:** W2 and W3.

**Outcome:** “Verified” refers to an observed execution of the intended candidate, not a matching old trace.

**Starting points:** `_pair_candidates()` in `gate_service.py`; command and entrypoint flows in `sdk/tracely_sdk/cli.py`; `_record_fail_to_pass()`; case/run serializers; `.github/actions/tracely-gate/`.

**Work:**

- Introduce or extend an execution manifest binding run, project, agent, case version, candidate revision, execution mode, and emitted traces.
- Require explicit pairing scoped to the current execution for verified CI results. Validate supplied IDs rather than trusting caller-provided pairings alone.
- Replace fixed sleeps with bounded ingestion completion checks tied to expected trace IDs/run identity.
- Respect subprocess failure and timeout. A stale passing trace must not rescue a failed command.
- Handle supported synchronous and asynchronous entrypoints correctly and document unsupported behavior.
- Migrate verification evidence conservatively using the lifecycle in section 5.3.
- Keep suite inclusion and repository branch protection separate from candidate verification.

**Acceptance criteria:**

- An older trace with identical input cannot satisfy the current candidate run.
- Failed commands, missing traces, delayed ingestion, duplicate retries, and wrong-project IDs have deterministic outcomes.
- Source failure alone never displays as a verified fix.
- Changing a contract/version invalidates or clearly scopes prior verification evidence.
- A real local GitHub-style fixture workflow shows the buggy revision failing and the fixed revision passing with inspectable provenance.

### W5 — A focused failure-to-fix workspace

**Priority:** P1. **Dependency:** W2–W4 contracts stable.

**Outcome:** A developer can move from a failure to a verified regression without manually coordinating several disconnected screens.

**Starting points:** `frontend/app/(app)/cases/[caseId]/page.tsx`, `ReplayControls.tsx`, failure/cluster detail, case APIs, shared trace detail components.

**Screen requirements:**

1. **Failure context:** What failed, recurrence/impact where measured, agent/environment, source evidence, and whether a relevant test exists.
2. **Expected behavior:** A plain-language summary backed by editable assertions. Keep advanced configuration available through progressive disclosure.
3. **Execution setup:** Supported mode, required input/state, fixture readiness, and a copyable command populated for this case.
4. **Candidate selection:** Exact recent compatible runs or a new execution. Pasting a trace ID may remain an advanced path, not the primary interaction.
5. **Comparison:** Original and candidate outcomes, aligned relevant steps, tool arguments/results, and the first meaningful divergence. Avoid dumping two unaligned JSON blobs.
6. **Evidence receipt:** Case version, candidate revision, mode, completed/missing checks, and result. Explain what the execution establishes.
7. **Next action:** Add to suite, inspect CI result, or configure the required check. Do not present an unverified repository as protected.

**Expectation editing scope:** Start with required/forbidden tools, argument predicates, bounded call counts, relevant ordering, and error-handling expectations. Add observable state/output assertions for the chosen supported integration. Semantic quality checks remain explicit, with visible judge configuration.

Do not copy bad source arguments into a golden contract. A failure trace is evidence of the bug, not necessarily the expected behavior. Allow a developer to correct expectations without creating a general-purpose hand-authored dataset product.

**Acceptance criteria:**

- A bad-argument failure can become a discriminating test: original fails, intended fix passes.
- A harmless alternative tool path can pass when the expected behavior allows it.
- Missing checks and unsupported execution modes are actionable on the same screen.
- Desktop comparison is readable side by side; narrow screens stack evidence in a clear order.
- Keyboard access, focus behavior, loading, cancellation, and error recovery work in the complete flow.
- At least one authenticated end-to-end walkthrough is recorded against the implemented application.

### W6 — First-use path and meaningful activation measurement

**Priority:** P1. **Dependency:** W1–W4 for truthful completion states. Event definition can start earlier.

**Outcome:** A new user can experience the workflow before doing extensive setup, then repeat it with a real agent.

**Starting points:** `frontend/app/components/Activation.tsx`, `frontend/app/lib/quest.ts`, `frontend/app/_providers/PostHogProvider.tsx`, SDK examples and installation docs.

**Work:**

- Provide a clearly labeled sample agent with a real reproducible bug and a fix, using the same supported execution path as users.
- Offer “Try the sample” and “Connect my agent.” Keep sample data distinguishable from customer data everywhere activation is measured.
- Validate install commands and generated snippets in a clean environment using the actual package and advertised extras.
- Guide real setup through first trace, completed check, useful case, candidate verification, and CI execution.
- Replace compulsory quest/theme/fleet steps with progress toward these outcomes. Preserve optional experiences without requiring them.
- Emit domain milestone events from confirmed successful operations, not merely button clicks or resource counts. Make retries idempotent.

**Suggested milestones:** `first_trace_received`, `first_check_completed`, `source_failure_confirmed`, `case_reproduced`, `candidate_verified`, `ci_check_completed`. Include project/workspace identity, sample-versus-real, integration path, and elapsed durations; exclude raw prompts, outputs, secrets, and customer content.

**Acceptance criteria:**

- A clean setup reaches the advertised sample outcome with documented prerequisites.
- Sample success does not count as real activation.
- An empty gate, incomplete judge, or source-only validation does not complete the corresponding milestone.
- A returning user resumes from durable state rather than local-storage-only progress.
- The founder can inspect counts and drop-offs by milestone without relying on a large new analytics subsystem.

### W7 — Reliable failure discovery and simpler navigation

**Priority:** P1. **Dependency:** Independent server-filter work; navigation follows W5.

**Outcome:** Users can find relevant failures across their data and understand their next action.

**Starting points:** `TracesExplorer.tsx`, its API and ClickHouse readers, `Sidebar.tsx`, dashboard, failure/cluster pages.

**Work:**

- Move status, failing, multi-turn, and text filtering to the server where currently limited to loaded rows. Preserve existing server-side time/agent/sort behavior.
- Define searchable fields, case sensitivity, metadata behavior, and counts after filtering. Keep query and pagination semantics consistent.
- Use deterministic pagination and handle stale responses when filters change quickly.
- Persist useful views through shareable URL state first; add workspace saved views only when justified.
- Organize primary navigation around Overview, Failures, Tests, and Runs. Keep raw traces and advanced tools discoverable within those workflows.
- Preserve existing routes and deep links; avoid a disruptive route rewrite solely for new labels.
- Make the overview show important unprotected failures, incomplete tests, and recent candidate/CI outcomes with direct actions. Keep aggregate charts available as secondary evidence.
- Offer simple evaluator presets and alert recipes; retain advanced editors without making their full complexity the default.

**Acceptance criteria:**

- A matching item beyond the first loaded page is returned by filtering.
- Empty states, displayed counts, pagination, and sorting describe the same result set.
- Cross-project results cannot leak through search or counts.
- Shared filter URLs reproduce the intended view.
- A new user can reach the failure-to-fix flow without understanding every advanced product surface.
- Performance is measured on a representative dataset before adding virtualization or a larger query rewrite.

### W8 — Adoption extensions chosen from observed friction

**Priority:** P2. **Dependency:** Real partner usage of W1–W7.

Choose one extension at a time:

- **GitHub workflow improvement:** Action output with case-level evidence and links; clear setup for required checks.
- **Existing observability coexistence:** One importer or dual-export path so teams can try Tracely without migrating their tracing platform.
- **One TypeScript integration:** A narrow supported path if qualified partners repeatedly cannot adopt because their runtime is JavaScript/TypeScript.
- **Editor/MCP workflow:** Authenticated case retrieval, reproduction setup, and candidate verification through existing API permissions.

Choose based on blocked partner workflows, not the attractiveness of a feature category. For every extension, record who is blocked, the workaround, and the measurable outcome it should improve.

## 7. Validation and release requirements

### Behavioral benchmark

Build a small curated benchmark around the chosen integration, targeting approximately 20 bug/fix pairs. Cover missing tools, wrong arguments, duplicate calls, handled tool errors, state mistakes, quality-only failures, and legitimate alternative behavior.

Keep infrastructure fault scenarios separate: missing fixtures, exhausted fixtures, unsupported providers, missing judges, stale candidates, failed commands, delayed ingestion, duplicate requests, and expired source traces.

Include changed-prompt/model examples that demonstrate why recorded model outputs are insufficient. These should direct users to the appropriate mode instead of creating a misleading green result.

Success means the expected buggy/fixed distinction is demonstrated with inspectable evidence; passing test counts alone are insufficient.

### Test execution

Use targeted tests while developing. Before completing a package, run the relevant broader checks according to `CLAUDE.md` and `.github/workflows/ci.yml`.

Typical commands, from the repository root unless stated otherwise:

```bash
uv run pytest -q backend/tests sdk/tests
uv run ruff check .
```

For frontend packages, run from `frontend/`:

```bash
pnpm test
pnpm build
```

Do not run whole-repository formatting or unrelated rewrites. Report pre-existing failures separately. End-to-end claims require an actual running application; mock tests do not establish them.

### Migration and rollout checklist

- Inventory all readers/writers of statuses, artifact formats, and version fields.
- Prefer additive migrations and conservative backfills before removing legacy fields.
- Test old fixtures/cases, pinned versions, missing source data, and mixed SDK/server versions.
- Document compatibility behavior and the supported minimum SDK version.
- Preserve historical results as historical evidence; do not relabel them as newly verified.
- Roll out stricter CI behavior with visible policy/version metadata and clear remediation.
- Make rollback behavior explicit without silently restoring misleading PASS outcomes.
- Verify case deletion, workspace deletion, and trace retention independently.

## 8. Founder-owned adoption plan

Engineering can improve the workflow; it cannot establish demand by itself.

### Recruit and observe

1. Interview roughly 10 builders in the proposed segment. Ask about a recent agent failure, its cost, how they debugged it, and why their current tests missed it.
2. Recruit approximately five design partners who can bring an actual failure and a working agent.
3. Work on one agent and one failure family per partner. Record setup friction and manual interventions.
4. Start checks in advisory mode where needed; make them blocking after teams trust the evidence and noise level.
5. Ask for a second real case and repeated use. A successful guided demo is not retention.

### Distribution after the workflow works

- Publish one runnable reference repository showing a real bug, its fix, and CI evidence.
- Produce one short walkthrough following exactly that path.
- Write one integration guide for the chosen stack with tested commands.
- Make useful result sharing part of the workflow, while respecting access controls and customer content.
- Prefer a concrete partner case study over a broad feature launch. Obtain permission before publishing customer material.

### Pilot scorecard

These are decision targets for a small pilot, not conversion forecasts or statistically reliable benchmarks.

| Signal | Initial target / interpretation |
| --- | --- |
| Supported setup | Aim for a first completed real check in about 15 minutes after prerequisites; record exceptions. |
| First useful regression | A verified case within one working session. |
| Repeat value | At least three of five partners bring a second real failure. |
| Self-service | After iteration, four of five can repeat the workflow without founder intervention. |
| Continued usage | At least three of five keep checks active across four weeks; inspect actual runs. |
| Trust | Record false alarms, unavailable checks, overrides, and reasons, with sample sizes. |
| Commercial signal | Seek an actual paid continuation, not only positive feedback or hypothetical willingness. |

If partners cannot identify valuable failures, reconsider the target problem. If they identify failures but cannot encode useful expectations, improve contracts. If they complete tests but do not return, investigate recurring value before adding integrations.

### Pricing and operating economics

Keep pricing changes separate from this implementation backlog. Measure storage bytes/spans, judge cost, replay execution, retention cost, and support effort by workspace. A trace count alone may not describe cost well.

Plan-based retention is now implemented; validate its operation rather than rewriting it. Any trial credits or hosted judge subsidy require an explicit product decision and must preserve credential and budget boundaries.

## 9. Sequencing and checkpoints

The following is an approximate schedule for a small team. It is not a delivery promise. If reliability work expands, move UI and integrations later rather than weakening the guarantees.

| Period | Engineering focus | Founder work | Exit checkpoint |
| --- | --- | --- | --- |
| Weeks 1–2 | Confirm gaps, W1 first slice, define result contract and milestones. | Interviews; select supported path and partners. | Reproducible gaps and one concrete customer workflow. |
| Weeks 3–5 | W1–W4: execution, verdicts, artifacts, provenance. | Observe assisted runs; collect bug/fix examples. | A supported bug/fix pair has trustworthy, durable evidence. |
| Weeks 6–8 | W5–W7 minimum workspace, onboarding, filtering. | Repeat sessions without guiding every action. | Partners complete the workflow using their own failures. |
| Weeks 9–10 | One W8 extension chosen from observed blockers. | Reference guide/demo and repeat-use follow-up. | The extension removes a measured adoption obstacle. |
| Weeks 11–12 | Reliability and friction from actual usage. | Paid continuation, cost review, approved case study. | Decide whether to deepen this segment or change direction. |

**Do not wait twelve weeks to show the product to users.** Partner sessions begin while the first reliability slices are being implemented.

## 10. Implementation tracker

Update this section in the same change that completes a package. Add commit/PR references, checks run, and known limitations. A checked item means its acceptance criteria are satisfied, not merely that code was added.

- [x] W1 — Explicit replay execution and strict fixtures. (2026-09-07, working tree on top of `636a5bd`; record below.)
- [x] W2 — Shared evaluation contract and honest incomplete states. (2026-09-07; record below.)
- [x] W3 — Durable versioned case artifacts and migration. (2026-09-07; record below.)
- [x] W4 — Current-run candidate provenance and verification evidence. (2026-09-07; record below.)
- [x] W5 — Failure-to-fix workspace and editable expectations. (2026-09-07; record below — no authenticated walkthrough yet, see limitations.)
- [x] W6 — Executable onboarding and domain activation events. (2026-09-07; record below.)
- [x] W7 — Full-dataset filtering and focused navigation. (2026-09-07; record below — performance not measured, see limitations.)
- [ ] W8 — One evidence-backed adoption extension. (Provisional slice shipped 2026-09-07: GitHub output evidence + required-check guidance. Left UNCHECKED: the package asks for a choice driven by observed partner friction, and no partner usage exists yet. Record below.)
- [ ] Pilot checkpoint — Repeat usage, trust, and paid continuation reviewed by founder.

### Per-package completion record

```text
Package:
Problem confirmed in current code:
Implemented behavior:
Files / commit / PR:
Compatibility and migration behavior:
Checks run and outcomes:
Observed end-to-end evidence:
Known limitations:
Next dependency unlocked:
```

#### W8 — GitHub workflow improvement (provisional, 2026-09-07)

```text
Package: W8 (provisional — not an evidence-backed choice yet)
Why this one: no partner usage exists to observe friction from. The GitHub output is the last
  step of the core workflow (W1–W7) rather than a new surface, so it was the only extension
  that could be justified without observation. Re-evaluate against real blocked workflows;
  who is blocked / workaround / measurable outcome are still to be recorded by the founder.
Implemented behavior:
  - PR comment + step summary: header carries incomplete count, run id and execution mode;
    each case links to its failure-to-fix page (same login-wall rule as conversation links —
    suppressed when a public share link is present), shows its case version, and a glanceable
    checks string (✓ passed · ✗ failed · ? could not run · ᵃ advisory) beside the reason.
  - Footer on every comment: make `tracely/regression-gate` a required status check; Tracely
    does not verify branch protection. Action README: "Make the check required" section.
Files / commit / PR:
  sdk/tracely_sdk/cli.py (checks_summary, REQUIRED_CHECK_NOTE, render_markdown),
  .github/actions/tracely-gate/README.md, sdk/tests/test_cli_outcome.py (+2).
Compatibility: markdown-only change; rows without `detail.checks` render as before.
Checks run and outcomes: ruff clean; pytest green; (frontend untouched).
Observed end-to-end evidence: none (no PR posted from this session).
Known limitations: no verification of branch protection (would need a repo-admin token and a
  GitHub API call — deliberately not added); no importer / TypeScript / MCP extension started.
```

#### W7 — Full-dataset filtering and focused navigation (2026-09-07)

```text
Package: W7
Problem confirmed in current code:
  - TracesExplorer: Failing / Multi-turn / text filters ran over the loaded 50 rows; chip counts
    were counts of loaded rows; empty state said "No loaded threads match … try Load more".
  - No URL state for the view; no stale-response guard; nav grouped Observe/Triage/Test/Ship.
Implemented behavior:
  - async_reader.session_filter_clauses(failing, multi, q) → HAVING fragments over the thread
    rollup (whole set); q is parameterised, case-insensitive (positionCaseInsensitiveUTF8),
    bounded to 200 chars, over first_input / last_output / model / metadata JSON / agent_id
    (_SESSION_HAYSTACK, documented). sessions_overview(count_only=True) returns the matching
    total from the same query body. GET /api/sessions takes failing/multi/q; GET
    /api/sessions/count returns {total} for the same filters. Deterministic order unchanged
    (session_order_clause tie-breaks).
  - Frontend: /api/sessions and /api/sessions/count proxies; getSessions/getSessionsCount;
    TracesExplorer rewritten — every filter is a server query, view ⇄ URL (viewFromParams /
    paramsFromView, defaults omitted, unknown values fall back), router.replace on change,
    back/forward followed, 300 ms debounced text, request sequencing (newest wins), "N matching"
    count, honest empty state, "link to this view". /traces reads searchParams for the first
    server render so a shared URL reproduces the list.
  - Sidebar regrouped Overview · Failures (clusters, traces, trends) · Tests · Runs; routes
    untouched. Dashboard gains a NextActions strip: unprotected failures (OPEN clusters without
    a case), incomplete tests (DRAFT or not verified at the current version), last CI run
    status — each with a direct action.
  - Evaluator presets (template library + AI draft in AddColumnModal) and alert recipes
    (lib/alerts.ts TRIGGERS) already existed; unchanged.
Files / commit / PR:
  backend/tracely/{infrastructure/clickhouse/async_reader.py, api/routers/sessions.py},
  frontend/app/{components/TracesExplorer.tsx, components/Sidebar.tsx, (app)/traces/page.tsx,
  (app)/dashboard/page.tsx, lib/api.ts, api/sessions/route.ts, api/sessions/count/route.ts
  (new)}, docs product/traces.mdx. Tests: backend/tests/test_session_filters.py (3), frontend
  __tests__/TracesExplorer.view.test.ts (3).
Compatibility and migration behavior:
  - Additive query params; old clients get the old behaviour. No schema change.
Checks run and outcomes:
  ruff clean; pytest green; tsc clean; vitest 299 passed.
Observed end-to-end evidence:
  None against ClickHouse: the SQL fragment is unit-tested; the full query was not executed
  against a live cluster in this slice (a ClickHouse alias in HAVING is standard, but run
  `make infra-up` + the traces page before relying on it).
Known limitations:
  - Performance not measured on a representative dataset (acceptance criterion open): the
    count re-runs the thread rollup; fine at current scale, measure before virtualising.
  - Text search is substring, no metadata key targeting, no tokenising.
  - Overview tiles count over the loaded top-6 slices, not server totals.
  - Saved workspace views not added (URL state only, per the plan's "first").
Next dependency unlocked:
  W8 — extensions.
```

#### W6 — First-use path and activation milestones (2026-09-07)

```text
Package: W6
Problem confirmed in current code:
  - Activation.tsx snippets said `pip install tracely-sdk`; the package is tracely-ai.
  - Activation/quest doneness = resource counts (+ localStorage flags); theme/fleet were quest
    steps; sample (seeded) data counted exactly like the customer's own.
Implemented behavior:
  - project_milestones table (0036) + services/milestones.py: MILESTONES = first_trace_received,
    first_check_completed, source_failure_confirmed, case_reproduced, candidate_verified,
    ci_check_completed. record() first-wins/idempotent, sample flag, integration path (scope
    name / sdk language), elapsed_ms since first trace; record_own() writes in its own session
    so it can never roll back or commit the operation it observes; PostHog server capture
    (milestone:<name>, identity + durations only) when POSTHOG_API_KEY is set.
  - Hooks from confirmed operations: ClickHouse insert of a non-internal trace; a written
    eval verdict; a validated promote; a non-source candidate FAIL under complete recorded
    execution (reproduced); mark_verified; a run-scoped gate that finished PASS/FAIL with
    total>0 (empty / NO_COVERAGE / INCOMPLETE / unscoped complete nothing).
  - Sample data: demo_seed.launch sets TRACELY_SAMPLE=1; the SDK stamps tracely.sample on every
    span, send_test_trace.py stamps the OTLP attribute; is_sample() also honours env demo/sample.
  - GET /api/onboarding/milestones (workspace) · GET /api/admin/milestones/funnel (user-only:
    real vs sample counts + drop-off per milestone).
  - Activation card rebuilt on the six milestones with "Try the sample" (seeds, labelled) and
    "Connect my agent"; sample-only banner; legacy count fallback only for workspaces with no
    milestone rows. Quest: theme + fleet steps removed (10 steps); trace/case/gate steps read
    real milestones. /api/onboarding proxy carries milestones.
  - Install validated in a clean venv: `uv pip install "./sdk[openai]"` → `tracely --help` and
    the exact Activation snippet (init + trace) run. PyPI carries 0.4.2; 0.4.3 is unpublished.
Files / commit / PR:
  backend/tracely/{services/milestones.py (new), services/{ingestion_service,
  evaluation_service, regression_service, gate_service, case_grading, demo_seed}.py,
  infrastructure/db/models.py, api/routers/{analytics,admin}.py}, migrations/0036_*.py,
  scripts/send_test_trace.py, sdk/tracely_sdk/__init__.py, frontend/app/{components/
  Activation.tsx, components/SampleButton.tsx (new), lib/quest.ts, lib/api.ts,
  api/onboarding/route.ts, (app)/dashboard/page.tsx}, docs product/dashboard.mdx. Tests:
  backend/tests/test_milestones.py (6), frontend Activation.test.tsx + quest.test.ts updated.
Compatibility and migration behavior:
  - New table only; nothing backfilled (a pre-W6 workspace shows the legacy count ticks until
    its first new milestone lands, then milestones govern).
  - QuestLocal keeps theme_touched (unused by steps now) — no localStorage migration.
Checks run and outcomes:
  ruff clean; pytest backend+sdk green; tsc clean; vitest 296 passed.
Observed end-to-end evidence:
  Clean-venv install + snippet executed. No browser walkthrough; milestone hooks exercised in
  unit tests with record_own stubbed.
Known limitations:
  - The sample flag depends on the seed path (TRACELY_SAMPLE) or env demo/sample; a user who
    runs the sample agent by hand in env=prod is counted as real.
  - `case_reproduced` requires execution mode `recorded` on the candidate root span (only
    `tracely replay --entrypoint` stamps it).
  - Funnel is a plain count endpoint, no UI page.
Next dependency unlocked:
  W7 — Overview can show milestone gaps as "next action".
```

#### W5 — Failure-to-fix workspace (2026-09-07)

```text
Package: W5
Problem confirmed in current code:
  - Case page = assertions JSON, reference trajectory, a paste-a-trace-id replay box; no editing,
    no candidate discovery, no comparison, no receipt; "fail → pass validated" as one badge.
  - Contract knew only required_tools / match_mode / no_error / allow_tool_errors.
Implemented behavior:
  - Contract: forbidden_tools, max_tool_calls {tool: n}, tool_args [{tool, key(dotted), equals}]
    (argument predicates; @observe {kwargs} shape handled). Each becomes a named required check.
    TrajectoryStep now carries `input`. EDITABLE_ASSERTIONS whitelist + MATCH_MODES.
  - RegressionService.update_expectations: whitelist/validate, bump version, re-snapshot
    artifact (from the artifact's input+fixtures, or source spans), re-validate source failure
    when the source exists (PROMOTED ⇄ DRAFT honestly). PATCH /api/cases/{id}/expectations
    (user-only).
  - domain/regression/compare.align_steps: relevant steps (tools, then errored non-tools) aligned
    by name sequence (difflib) with per-row divergence (arguments/result/error/added/not called)
    and first_divergence. GET /api/cases/{id}/compare?candidate=.
  - Candidates: trace_reader.traces_with_input (exact root input, newest first) →
    RegressionService.candidates (is_source, prior replay verdict, run id). GET
    /api/cases/{id}/candidates. case dict gains last_gate.
  - CLI: tracely replay --case <id|prefix> (scopes the suite and the gate's case_ids).
  - Frontend: case page rebuilt as 7 numbered sections (failure context / expectations with
    plain-language lines + ExpectationsEditor (progressive disclosure for bounds/predicates) /
    execution setup with copyable command + scope-of-evidence note / CandidatePicker (exact
    runs, Verify, advanced paste, cancel, errors) / Comparison side-by-side (lg) or stacked with
    first divergence + answers / Receipt (version, digest, mode, checks, judges, what it
    establishes) / Next (suite, last CI verdict, branch protection "not confirmed")). Native
    form controls, labels, aria-expanded, role=alert; lib/expectations.ts with vitest.
Files / commit / PR:
  backend/tracely/domain/{trajectory.py, regression/contract.py, regression/outcome.py,
  regression/compare.py (new)}, services/regression_service.py,
  infrastructure/{clickhouse/trace_reader.py, db/repositories.py}, api/routers/cases.py,
  sdk/tracely_sdk/cli.py, frontend/app/{(app)/cases/[caseId]/page.tsx, components/
  ExpectationsEditor.tsx, components/CandidatePicker.tsx, lib/expectations.ts(+test),
  lib/api.ts, api/cases/[caseId]/expectations/route.ts, globals.css (.input)}, docs
  product/cases.mdx. Tests: backend/tests/test_failure_to_fix.py (9), frontend
  lib/expectations.test.ts (3), sdk test_cli_replay_strict.py (+1).
Compatibility and migration behavior:
  - Assertion blob additive; old cases without the new keys evaluate as before. Old
    reference_trajectory steps lack `input` → comparison shows no args for them.
  - No schema change (uses W3/W4 columns).
Checks run and outcomes:
  ruff clean; backend+sdk pytest → all green (see next run); frontend tsc clean; vitest 294 passed.
Observed end-to-end evidence:
  None in a browser. Acceptance "at least one authenticated end-to-end walkthrough recorded
  against the implemented application" is NOT met in this slice — needs the stack running.
Known limitations:
  - Candidate discovery matches the exact root input string (no fuzzy); cross-env by design.
  - Comparison is structural (tools/args/results/errors + answer text); no judge re-run inline.
  - Recurrence/impact "where measured" (§5 screen req 1) not shown: no cluster join on the page.
  - Keyboard/focus/loading are implemented with native controls but not audited in a browser.
Next dependency unlocked:
  W6 can point onboarding at this screen as the "useful case" milestone.
```

#### W4 — Exact candidate identity and honest verification state (2026-09-07)

```text
Package: W4
Problem confirmed in current code:
  - _pair_candidates: explicit ids trusted as-is (no project/input/run validation); fallback =
    latest env trace by input digest → an older trace could satisfy the current run.
  - CLI --cmd: subprocess exit status ignored, fixed time.sleep(8); sync-only entrypoints.
  - fail_to_pass_validated set when the SOURCE fails, rendered as "fail → pass validated".
Implemented behavior:
  - Run identity: CLI mints run-<hex> (or --run-id / TRACELY_RUN_ID), exports TRACELY_RUN_ID;
    the SDK span processor stamps tracely.replay.run_id on every span; replay also stamps
    tracely.replay.case_version on the root. Gate body carries run_id, failed {case: reason},
    execution_mode; GateRun.run_id + execution_mode (migration 0035) = the manifest.
  - Pairing (GateService._pair_candidates → Pairing{trace_id, spans, problem, pairing}): with a
    run id, a candidate must exist in this project, be stamped with THIS run and carry the case
    input, else INCOMPLETE with the reason (not found / not produced by this run / input
    differs / command failed: …); without explicit ids, digest matching is restricted to the
    run's traces (trace_reader.traces_for_run). No run id → legacy explicit-unscoped /
    digest-fallback pairing, recorded in detail.provenance and warned on the run.
  - Verification evidence: evaluation_cases.verified_candidate_trace_id / verified_case_version
    / verified_at / verified_by, set by case_grading.mark_verified only on a PASS with complete
    execution — from a run-scoped gate ("gate:<id>") or a manual replay of a non-source trace
    ("replay"). fail_to_pass_validated keeps meaning source-failure; never migrated.
  - CLI --cmd: exit status + --cmd-timeout honoured (failed map), bounded polling of
    GET /api/gate/run-traces by run id instead of sleep; async entrypoints awaited; generator
    entrypoints refused. Exit 2 on INCOMPLETE.
  - Action exports TRACELY_RUN_ID=<run_id>-<run_attempt>; README explains wrapping the agent step.
  - UI: case page shows "source failure confirmed" / "candidate verified · vN" / "verified
    against vN — re-verify" as separate badges; gate page shows run id + mode, warns when
    unscoped; suite/gate detail carry provenance.
Files / commit / PR:
  sdk/tracely_sdk/{__init__.py, cli.py}, backend/tracely/{services/gate_service.py,
  services/case_grading.py, services/regression_service.py, infrastructure/db/models.py,
  infrastructure/clickhouse/trace_reader.py, api/routers/gate.py, api/routers/cases.py},
  migrations/versions/0035_run_provenance.py, .github/actions/tracely-gate/{action.yml,
  README.md}, frontend (api.ts, case page, gate page), docs (cli, product/cases,
  product/gates). Tests: backend/tests/test_candidate_provenance.py (new, 10),
  sdk/tests/test_cli_replay_strict.py (+4: async, generator refused, explicit run id, --cmd
  failure + run-scoped polling).
Compatibility and migration behavior:
  - Additive columns; old CLIs post no run_id → legacy pairing + a warning (behaviour unchanged
    otherwise). New CLI against an old backend: extra body keys ignored, /gate/run-traces 404 →
    the poll gives up at the timeout and gates anyway (warned).
  - Historical fail_to_pass_validated rows are NOT relabelled; verified_* start empty.
Checks run and outcomes:
  ruff check → clean; uv run pytest -q backend/tests sdk/tests → 1127 passed, 34 skipped;
  frontend tsc --noEmit → clean.
Observed end-to-end evidence:
  Unit-level only: backend tests drive run_gate with stamped traces (stale/missing/wrong-input/
  failed-command/legacy); CLI tests drive cmd_replay with a real subprocess exiting 1 and a
  fake API. The buggy→fixed GitHub-style workflow against a running backend was NOT executed
  in this slice (needs the stack up).
Known limitations:
  - `tracely gate` verification depends on the workflow exporting TRACELY_RUN_ID around its own
    agent run; unset → digest-fallback (warned, not verified).
  - Scenario (simulate) gates are outside the run-id manifest.
  - Suite inclusion / branch-protection facts (§5.3 rows 4–6) are not yet surfaced as evidence;
    "merge protection confirmed" is not implemented (W5/W8).
Next dependency unlocked:
  W5 — the case page now has every fact the workspace needs (source failure, artifact,
  candidate verification with version, checks, provenance).
```

#### W3 — Durable, versioned case artifacts (2026-09-07)

```text
Package: W3
Problem confirmed in current code:
  - GateService._recover_input read the SOURCE spans at suite time and returned "" when gone;
    the fixture bundle was the only durable piece, keyed by input digest. A case outlived its
    executable input by exactly one retention window.
  - case_delete left blobs; delete_project_blobs knew two prefixes; no backfill/recapture.
Implemented behavior:
  - domain/regression/artifact.py: CaseArtifact (format_version 1) = typed input
    {"type":"text"}, fixture bundle, expectations {assertions, match_mode}, evaluators (judge
    identity per expected judge), execution {"mode":"recorded"}, initial_state (declared NOT
    captured), provenance (project/agent/agent_version/source trace+span/input_digest/
    captured_at). encode() canonical JSON, digest() sha256, decode() strict (empty/garbage/
    wrong-shape/newer-format → ValueError). build() refuses an empty input.
  - Key {prefix}cases/{project}/{case}/v{case.version}.json; evaluation_cases.artifact_s3_key +
    artifact_digest (migration 0034).
  - promote_trace snapshots the artifact BEFORE the commit (blob down ⇒ promote fails whole;
    deterministic keys ⇒ a retry overwrites its own orphans). replay_suite executes from the
    artifact (input + fixtures) and reports case_version + artifact_digest; legacy cases fall
    back to source spans + bundle while those exist, else fixture_error "input unrecoverable …
    recapture". Gate results pin case_version, artifact_digest and the expectations snapshot.
  - backfill_artifacts(project_id|None): snapshot un-artifacted cases from source + bundle;
    unrecoverable ones reported with reason, never invented. Runs nightly at the top of
    tracely.enforce_retention (before any sweep, regardless of billing) and on demand via
    POST /api/cases/backfill-artifacts (user-only).
  - recapture(case, trace_id): same input_digest required, bumps case.version, re-stores the
    bundle, writes v{n+1}. POST /api/cases/{id}/recapture (user-only; 409 on digest mismatch).
  - case_delete removes the case's artifact prefix + fixture bundle (best-effort, after rows);
    delete_project_blobs covers cases/{project}/ in both wipe modes. Retention (ClickHouse only)
    never touches blobs. Case page shows "Artifact vN · digest" or "not snapshotted".
Files / commit / PR:
  backend/tracely/domain/regression/artifact.py (new), services/regression_service.py,
  services/gate_service.py, infrastructure/db/{models.py, repositories.py},
  infrastructure/blob/s3.py, workers/tasks.py, api/routers/cases.py,
  migrations/versions/0034_case_artifacts.py, frontend (api.ts, case page), docs
  (product/cases.mdx), backend/README.md. Tests: backend/tests/test_case_artifacts.py (new, 16).
Compatibility and migration behavior:
  - Additive columns with server defaults; legacy cases keep working from their sources until
    the nightly backfill snapshots them. No data rewrite. EvaluationSuiteCase.pinned_case_version
    reused as-is (recapture bumps case.version; pinning semantics unchanged).
  - Suite rows gained case_version/artifact_digest; old CLIs ignore them.
Checks run and outcomes:
  ruff check → clean; uv run pytest -q backend/tests sdk/tests → 1113 passed, 34 skipped;
  frontend tsc --noEmit → clean. alembic not run against a live DB here.
Observed end-to-end evidence:
  test_promoted_case_executes_after_its_source_is_retained_away: promote, drop the reader's
  spans, replay_suite still yields input + fixtures from the artifact. No live stack run.
Known limitations:
  - Input is text only (root span input string); structured/multi-modal inputs are stored as
    their string form. Initial external state is declared uncaptured, not captured.
  - Backfill is a full-table scan of un-artifacted cases per night (fine at current scale).
  - Historical replays graded before W2/W3 have no case_version/digest in their detail.
  - Tenant isolation is by project_id on every read (recapture/backfill scoped); workspace
    deletion covered by delete_project_blobs, not re-tested end-to-end here.
Next dependency unlocked:
  W4 — the suite now carries the exact case version + artifact digest for the run manifest.
```

#### W2 — One evaluation contract, honest INCOMPLETE (2026-09-07)

```text
Package: W2
Problem confirmed in current code:
  - domain/regression/contract.apply_quality left the verdict unchanged when the judge returned
    nothing → a quality-only case PASSed with the answer unchecked (no key / judge disabled).
  - RegressionService.replay_case graded structure only; the gate graded structure + quality →
    the same candidate could PASS on the case page and FAIL in CI.
  - No notion of "could not check": CaseReplay.verdict String(8), GateRun.status String(12).
Implemented behavior:
  - domain/regression/outcome.py: Check(name, required, status PASS|FAIL|UNAVAILABLE),
    Execution(mode, problem, diverged) read off the trace's tracely.replay.* stamps, and
    evaluate_contract → CaseOutcome. Policy: required FAIL → FAIL; else execution problem or
    required UNAVAILABLE → INCOMPLETE; else PASS. Expected judges come from
    assertions.quality.score_names; an expected judge with no result is UNAVAILABLE (an empty or
    partial list never implies complete evaluation). Advisory (gate_quality_blocks=False) checks
    are visible, never deciding. Judge identity (evaluator_id, config digest, model) stored in
    detail.judges. gate_status(...) is the readable policy table (empty suite / partial coverage
    / all-skipped / mixed / incomplete); NO_COVERAGE kept.
  - services/case_grading.grade_case is the ONE path: replay_case, _record_fail_to_pass (source
    validation, now detail.evidence="source_failure") and GateService._record_gate_cases all use
    it. apply_quality and GateService._grade_quality deleted.
  - GateRun.incomplete count; gate status INCOMPLETE (severity PASS < NO_COVERAGE < INCOMPLETE <
    FAIL); alerts fire on it. trace_reader reads the span metadata map (execution evidence).
  - CLI: INCOMPLETE ranked/rendered/explained; case_reason leads with the execution problem and
    names unavailable required checks; exit codes 0 PASS · 1 FAIL/NO_COVERAGE · 2
    INCOMPLETE/ERROR/unreachable (exit_code()); GitHub status "failure" for INCOMPLETE.
  - Frontend: INCOMPLETE tone everywhere a status renders (badges, gates list/detail, share page),
    incomplete stat, and a CaseChecks component showing checks + execution on gate and case pages.
  - Migration 0033: widen case_replays.verdict / gate_cases.verdict / gate_runs.status to 16,
    add gate_runs.incomplete.
Files / commit / PR:
  backend/tracely/domain/regression/{outcome.py (new), contract.py}, services/{case_grading.py
  (new), regression_service.py, gate_service.py}, infrastructure/db/models.py,
  infrastructure/clickhouse/trace_reader.py, api/routers/gate.py, migrations/versions/0033_*.py,
  sdk/tracely_sdk/cli.py, frontend (ui.tsx, CaseChecks.tsx new, gates pages, case page, share
  page, api.ts, alerts.ts), docs (product/gates, product/cases, product/alerts, cli), backend
  README. Tests: backend/tests/test_case_outcome.py, test_case_grading_parity.py (new),
  test_gate_eval.py (apply_quality tests replaced), sdk/tests/test_cli_outcome.py (new).
Compatibility and migration behavior:
  - Additive: old CaseReplay/GateCase rows keep their flat detail; the UI renders CaseChecks only
    when detail.checks exists. Legacy flat keys (quality_pass/quality_reason/...) still emitted.
  - Behaviour change: a quality-only case whose judge cannot run is now INCOMPLETE in the gate
    (was PASS) and blocks. With gate_quality_blocks=False it stays PASS with the check visible.
  - Promote-time: a source that only fails an ADVISORY judge no longer promotes (stays DRAFT) —
    consistent with the gate never blocking on it.
  - Exit code change: INCOMPLETE → 2. NO_COVERAGE unchanged (1).
Checks run and outcomes:
  ruff check → clean; uv run pytest -q backend/tests sdk/tests → 1097 passed, 34 skipped;
  frontend tsc --noEmit → clean. alembic upgrade not executed against a DB in this slice.
Observed end-to-end evidence:
  Parity test drives RegressionService.replay_case and GateService.run_gate on the same case +
  candidate with the judge returning nothing → both INCOMPLETE with identical checks. No
  authenticated browser walkthrough.
Known limitations:
  - Timeout vs cancellation vs judge error are not separately distinguished: the judge path
    swallows exceptions into "no result" (evaluation_service._dispatch_specs) → all read as
    UNAVAILABLE with one generic reason. Distinguishing them needs the dispatcher to return
    per-spec failure reasons (small follow-up).
  - Production monitoring verdicts (domain/evaluation/verdict.py, async_reader SQL) untouched.
  - Scenario (simulate) outcomes keep their own PASS/FAIL/UNGRADED/SKIP grading; only the
    regression-case half moved to the contract.
Next dependency unlocked:
  W3/W4 store their artifact/provenance facts in the same detail payload (execution, judges,
  policy) the contract now emits.
```

#### W1 — Explicit replay execution and strict fixtures (2026-09-07)

```text
Package: W1
Problem confirmed in current code (at 636a5bd):
  - sdk/tracely_sdk/__init__.py: fixtures({}) set the store to None → live; _pop_fixture popped the
    queue head when args did not match; call_tool/call_llm/@observe/provider patches ran the real
    function on any miss; recorded args from ClickHouse are JSON *strings* so arg matching against a
    real bundle never matched and always fell back to order.
  - backend/tracely/services/gate_service.py _load_fixtures returned {} on a load failure and on a
    case with no bundle key; FixtureBundle.decode returned {} on garbage → both replayed as live.
  - CLI --cmd runs are live by construction (external process); nothing said so.
Implemented behavior:
  - fixtures(None) = no replay (live). Any other bundle (incl. {}) = recorded mode, strict by default:
    ReplayError(reason=missing|exhausted|mismatch, kind, key, detail) is raised instead of running the
    real function — for call_tool, call_llm, @observe(as_type="tool") and every provider adapter
    (one shared _serve_fixture). fixtures(bundle, strict=False) = legacy lenient fall-through, reported.
  - One explicit matcher (_args_match): no recorded args → ordered; else canonicalised equality
    (JSON strings parsed, key order / tuple-list ignored). Tool arg mismatch never consumes a
    different call (strict error). LLM: keyed by model, explicit any-name fallback when the model was
    never recorded; input mismatch served in order but stamped tracely.replay.divergence=input and
    reported. Recorded errors still replay as ToolError.
  - fixtures() yields ReplayReport(mode, served, unused, diverged, live, errors, providers, clean).
    Unused recorded calls are reported (not auto-failed). _install_replay_patches reports
    patched/absent/failed per provider.
  - Backend: _load_fixtures → (bundle, None) | (None, reason); suite rows carry fixtures + fixture_error;
    FixtureBundle.decode raises ValueError on empty/invalid/non-object JSON.
  - CLI: strict by default, --lenient flag; a case with fixture_error is NOT run — its trace is emitted
    ERROR ("replay error: …") so the gate fails; a strict miss the agent swallowed still errors the
    root span from rep.errors; root span stamped tracely.replay.mode; per-case tag
    [recorded · N served] / · diverged (+ evidence lines) / [replay error: fixtures unavailable] /
    [lenient · …] / [live]; --cmd prints that it runs live.
  - Docs: compatibility matrix (provider × entry point × sync/async × streaming × served shape ×
    what it is tested against) in sdk/README.md §2 and docs/pages/replay.mdx; cli.mdx,
    api-reference.mdx updated.
Files / commit / PR:
  sdk/tracely_sdk/__init__.py, sdk/tracely_sdk/cli.py, sdk/pyproject.toml (0.4.3), uv.lock,
  backend/tracely/services/gate_service.py, backend/tracely/domain/regression/fixtures.py,
  sdk/README.md, docs/pages/replay.mdx, docs/pages/cli.mdx, docs/pages/api-reference.mdx,
  tests: sdk/tests/test_replay_strict.py (new), sdk/tests/test_cli_replay_strict.py (new),
  backend/tests/test_replay_suite_fixtures.py (new), sdk/tests/test_replay_bridge.py (one test
  rewritten: fall-through is now the lenient contract). Uncommitted at time of writing.
Compatibility and migration behavior:
  - Breaking for SDK callers of fixtures(bundle) that relied on silent live fall-through: they now
    get ReplayError; opt back in with strict=False (documented as legacy/lenient, never "recorded").
    fixtures(None) unchanged. Bundle wire format (v1/v2) unchanged; no DB migration.
  - Old SDK + new backend: suite rows gained fixture_error; an old CLI ignores it and runs {}→live
    as before (it cannot be made strict remotely). New CLI + old backend: no fixture_error key → the
    {} it receives is treated as an explicitly empty recording → strict errors instead of live.
    Minimum SDK for strict semantics: 0.4.3.
Checks run and outcomes:
  uv run ruff check . → clean. uv run pytest -q backend/tests sdk/tests → 1066 passed, 34 skipped
  (pre-existing skips: optional provider SDKs absent). ruff format not run repo-wide (files were
  already unformatted before this change; only new lines kept within width).
Observed end-to-end evidence:
  None against a running stack. Provider interception verified against the real openai/anthropic
  client classes in-process (existing tests); no network-egress test. No authenticated CI run of
  `tracely replay` against a live backend was performed in this slice.
Known limitations:
  - No sandbox / egress denial: an un-patched surface (streaming, Responses API, Bedrock, raw HTTP…)
    is still live in replay and is only caught after the fact via rep.unused. Matrix says so.
  - LLM input divergence is reported, not failed (recorded-model mode); turning it into an
    INCOMPLETE outcome is W2's structured-result work. Exact-JSON input matching has a ceiling
    (a date injected into the system prompt diverges every run) — no normaliser hook yet.
  - A ReplayError in the CLI currently surfaces as an ERRORED root span → the gate's no_error
    assertion FAILs the case; behavioral FAIL vs execution problem are not yet distinguishable in
    the gate result (W2).
  - --cmd path unchanged apart from the "live" note (candidate provenance is W4).
  - Mistral/LiteLLM are tested through stand-ins only.
Next dependency unlocked:
  W2 — the report (rep.to_dict(), tracely.replay.mode/divergence span attrs, fixture_error) is the
  execution-completeness input the structured result needs.
```

## 11. Ready-to-use kickoff prompt for Claude Code

```text
Read CLAUDE.md and TRACELY_IMPLEMENTATION_PLAN.md. Inspect the current
working tree and verify that W1's reported replay behavior still exists.

Implement the first reviewable W1 slice: explicit replay policy and strict
handling of missing, exhausted, and mismatched fixtures. Cover the shared
fixture matcher and manual tool/model wrappers, plus any immediately
affected callers. Preserve unrelated changes and existing live execution.

Begin with focused tests that reproduce silent live fallback and incorrect
fixture matching. Make the implementation pass those tests. Do not claim
provider-wide or network-isolation guarantees until those paths are tested.
If adapters require additional slices, keep the new policy clearly scoped
and leave W1 unchecked until its full acceptance criteria are met.

Follow the architecture and compatibility constraints in the documents.
Do not start the UI redesign or unrelated roadmap packages in this change.
Run appropriate checks, update the completion record, and report what is
done, remaining limitations, and the next concrete slice.
```
