# Evaluation — the code map

Companion to [`EVALUATION_GUIDE.md`](EVALUATION_GUIDE.md) (what the product does) and
[`backend/README.md`](backend/README.md) §2 (the flow in prose). This one is **only** the code:
entrypoints, call chain, data shapes, and which file owns which rule.

All paths are relative to `backend/tracely/`.

---

## 1. The four entrypoints

Everything funnels into **one** class, `EvaluationService` (`services/evaluation_service.py`).
There is no second engine — the gate, the UI Play button and the ingest worker all call these
same three public methods.

| # | Trigger | Entry function | Calls |
|---|---|---|---|
| 1 | **Ingest (automatic)** | `workers/tasks.py:49` `evaluate_run_task` | `evaluate_trace(..., skip_conversation=True, execution_mode="batch")` |
| 1b | ↳ thread settled | `workers/tasks.py:90` `evaluate_conversation_task` | `evaluate_thread(..., execution_mode="sequential")` |
| 2 | **UI "Play" (on demand, SSE)** | `api/routers/evaluations.py:54` `POST /api/evaluations/run` | `evaluate_thread` / `evaluate_trace` with explicit `specs` |
| 3 | **CI gate — scenarios** | `services/gate_service.py:660,670` | `evaluate_trace` per turn + `evaluate_conversation` per thread |
| 4 | **CI gate — regression replay** | `services/gate_service.py:762` `_grade_quality` | `grade_trace_quality` (**non-persisting**) |

`services/regression_service.py:152` also uses `grade_trace_quality` when promoting a trace into a
case. Monitors (`services/monitoring_service.py`) *read* scores; they never produce them.

---

## 2. The ingest call chain, line by line

```
POST /v1/traces                       api/routers/otlp.py:16
  └─ ingest_otlp()                    services/ingestion_service.py:30
       ├─ S3 put (durable first)      infrastructure/blob/s3.py
       └─ ingest_otlp_blob.delay()    → Celery

Celery: ingest_otlp_blob              workers/tasks.py:20
  ├─ IngestionService.process_blob()  services/ingestion_service.py:46
  │    ├─ parse_otlp_traces(_json)    otel/
  │    ├─ insert_rows("events")       infrastructure/clickhouse/
  │    └─ returns {trace_ids, internal_trace_ids}
  └─ for trace_id not in internal_trace_ids:        ← GUARD #1 (no eval of Tracely's own traces)
       gen = eval_debounce.bump()     infrastructure/queue/eval_debounce.py:51
       evaluate_run_task.apply_async(countdown=settings.eval_debounce_seconds)   # default 4s

Celery: evaluate_run_task             workers/tasks.py:49
  ├─ eval_debounce.is_latest(gen)?    eval_debounce.py:65   ← trailing debounce; else return "superseded"
  ├─ EvaluationService.evaluate_trace(skip_conversation=True, execution_mode="batch")
  ├─ RollingSummaryService.build_for_thread()   (best-effort, never fails the run)
  └─ if result["needs_thread_pass"]:  ← only when the project HAS conv or sequential columns
       evaluate_conversation_task.apply_async(countdown=4)   # debounced on the THREAD, key "conv:{thread}"

Celery: evaluate_conversation_task    workers/tasks.py:90
  └─ EvaluationService.evaluate_thread(execution_mode="sequential")
```

Two debounce keys, one Redis counter (`tracely:eval:gen:{project}:{trace}`), namespaced so a trace
id and a thread id can't collide (`_CONV_KEY = "conv:{thread}"`, `tasks.py:46`).
Debounce **fails open**: any Redis error returns generation `0` = "always run"
(`eval_debounce.py:40 _should_run`). Over-evaluating is free (idempotent ids); under-evaluating
silently loses scores.

---

## 3. Inside `EvaluationService`

### `evaluate_trace()` — `evaluation_service.py:196`

```python
spans = trace_reader.read_spans(project_id, trace_id)       # trace_reader.py:47
if any(s.get("internal_kind") for s in spans): return       # GUARD #2 — line 213
root      = root_span(spans)                                # domain/traces/spans.py:19
thread_id = first non-empty conversation_id, else trace_id

specs = load_enabled_evaluators()  if specs is None         # line 612 → Postgres
specs = _apply_targeting(...)      on the auto path only    # line 330
trace_specs = [s for s in specs if s.level != CONVERSATION]
trace_specs = filter by execution_mode when the caller asked for one

if _needs_thread_context(trace_specs):                      # line 159
    thread_spans = trace_reader.read_thread_spans(...)      # ONE extra read, gated

ctx     = RunContext(...)                                   # domain/evaluation/results.py:34
results = _dispatch_specs(trace_specs, ctx)                 # line 515  ← THE chokepoint
score_writer.write_eval_scores(...)                         # ClickHouse
_emit(on_result, ...)                                       # SSE frames
if FAILs: _cluster_failure(...)                             # structural clustering, swallowed on error
if conv_specs: _evaluate_conversation(...)
```

Return value: `{scores, failures, thread_id, needs_thread_pass}`. `needs_thread_pass` is answered
here (the specs are already loaded) so the worker can skip queuing a task that would have nothing
to do — see `evaluation_service.py:265` for the reasoning.

### `evaluate_thread()` — `evaluation_service.py:275`

Loops `trace_reader.thread_trace_ids()` oldest→newest, calling `evaluate_trace` per turn, then
`_evaluate_conversation` once. The sequential chain lives here: `capture()` (line 304) stashes each
persisted score into `chain[score_name]` via `_chain_payload` (line 97), and `_with_previous`
(line 112) injects it into the next turn's spec as `config.__previous_result__`.

### `_dispatch_specs()` — `evaluation_service.py:515`

The single place every evaluator actually runs. In order:

1. `_topo_sort(specs)` (line 121) — orders by `config.depends_on`; a cycle logs
   `eval_dependency_cycle` and falls back to creation order.
2. `provider.use_project_key(project_id)` — the workspace's OWN OpenRouter key for every LLM call inside; no key configured = no LLM (server keys never apply inside a project scope).
3. `introspection.record(EVAL, ...)` — records the whole dispatch as a Tracely-internal trace
   (`stable=True` ⇒ re-runs replace, not stack).
4. Per spec: `_inject_dependencies` (line 148) → `registry.dispatch(...)` → collect results into
   `completed[score_name]` keyed by `span_id`.
5. Every evaluator is wrapped in `try/except` — one bad column can't sink the rest
   (`evaluator_failed` warning).

---

## 4. The registry and the evaluators

`domain/evaluation/evaluators/base.py`

```python
Evaluator(ABC)                  # line 31 — ClassVars: kind, check, default_level
EvaluatorRegistry.resolve()     # line 72 — (kind, config["check"]) → falls back to (kind, None)
EvaluatorRegistry.dispatch()    # line 80 — instantiate, stamp level + score_name, run, stamp names
default_registry                # line 105 — populated by @default_registry.register decorators
```

Adding a check = subclass `Evaluator`, set two ClassVars, decorate. No `if/elif` to touch.

| Class | `check` | Level | File:line |
|---|---|---|---|
| `RunOutcomeEvaluator` | `run_outcome` | AGENT_RUN | `structural.py:20` |
| `ToolSuccessEvaluator` | `tool_success` | TOOL (one result per tool span) | `structural.py:36` |
| `ToolConsistencyEvaluator` | `tool_consistency` | AGENT_RUN | `structural.py:57` |
| `LatencyEvaluator` | `latency` | AGENT_RUN | `structural.py:132` |
| `RequiredToolsEvaluator` | `required_tools` | AGENT_RUN | `structural.py:157` |
| `LLMJudgeEvaluator` | `None` (one class, all configs) | any | `llm_judge.py:271` |

`ToolConsistencyEvaluator._executed_via_generation_messages` (line 89) is the subtle one: it walks
`{role:"tool"}` messages and matches `tool_call_id` ↔ the assistant's `tool_calls[].function.name`,
so a tool loop that wasn't span-wrapped doesn't FAIL falsely.

### `LLMJudgeEvaluator` dispatch tree — `llm_judge.py:278`

```
run(ctx, params)
  ├─ provider.llm_enabled() is False → []            ← no key ⇒ degrade, never crash
  ├─ config["is_advanced"] → _run_advanced           :508  (template path)
  ├─ level == CONVERSATION → _run_conversation       :459  (turn-by-turn transcript, 8k clip)
  ├─ level in STEP_LEVELS  → _run_steps              :399  (one grade per span, max_spans=30)
  └─ else                  → _run_trace              :326  (request + answer + tool grounding)
                                    ↓
                        _grade      :583   (rubric = system prompt, + previous/deps blocks)
                   or  _grade_advanced :618 (resolved template = the human message, once)
                                    ↓
                        _call_and_build :641  → provider.run_structured_agent / run_text_agent
                                    ↓
                        _to_result :706  |  _json_result :741   → EvalResult
```

Context builders worth reading: `_step_candidates` (:368, excludes the root span, applies the
`max_spans` cap and reports coverage in the comment), `_capabilities` (:295, injects the declared
agent/tool catalog at step + conversation level only — deliberately *not* message level), and
`_turn_lines` (:145, with `stop_before` so a sequential message judge sees the turns *before* the
one it grades).

Advanced mode: `_run_advanced` → `template_resolver.build_context()`
(`domain/evaluation/template_resolver.py:402`, materializes only the referenced vars) →
`template_resolver.resolve()` (:540). The `@VARIABLE` catalog is `TEMPLATE_VARIABLES` (:101);
`CONVERSATION_SCOPED_VARS` (:388) is what `_needs_thread_context` keys off.

Durable judge chats (`_chat_id`, :238) apply to **sequential basic** columns only — returns `None`
when the checkpointer is unreachable, and that's load-bearing: the callers stop pasting history in
once a chat exists. `_grade_advanced`'s docstring (:618) explains why advanced columns deliberately
never chat.

---

## 5. Data shapes

**Spec** — the runner's view of an evaluator row. Built by
`infrastructure/db/repositories.py:187 evaluator_enabled_specs`, ordered by `created_at`, and
transitively expanded to include enabled `depends_on` prerequisites.

```python
{"id", "kind", "config", "score_name", "level", "target_agent", "target_env", "sampling"}
```

Runner-injected keys inside `config` (never persisted): `__previous_result__` (sequential seed),
`__dependencies__` (`{score_name: [{span_id, payload}]}`).

**`RunContext`** — `domain/evaluation/results.py:34`. `spans` is the trace's spans, or the whole
thread's for a CONVERSATION run. `thread_spans` is `None` unless fetched.

**`EvalResult`** — `results.py:19`: `name, level, verdict ("PASS"|"FAIL"|""), data_type, value,
string_value, target_span_id, comment, usage`.

**Score row** — `infrastructure/clickhouse/score_writer.py:76 write_eval_scores`. The identity rule:

```python
CONVERSATION → uuid5(NS, f"thread:{thread_id}:{name}")     trace_id = NULL, session_id = thread
otherwise    → uuid5(NS, f"{trace_id}:{name}:{span_id}")
NS = c0ffee00-0000-0000-0000-000000000001
```

`ReplacingMergeTree` + deterministic id = re-running replaces. Judge token usage lands in
`metadata` as `eval.input_tokens` / `eval.output_tokens` / `eval.total_tokens` / `eval.model`
(`_usage_metadata`).

---

## 6. Where each cross-cutting rule lives

| Rule | Owner | Note |
|---|---|---|
| **Verdict policy** | `domain/evaluation/verdict.py:20,26` | FAIL iff a non-advisory FAIL. SQL twin in `async_reader` (`name NOT IN {adv}`), advisory set from `api/advisory.py`. **Change both together.** |
| **Targeting + sampling** | `domain/evaluation/targeting.py:26 spec_applies` | Pure. Bucket = `sha256(f"{trace_id}:{score_name}")` → deterministic. Applied only on the auto path (`_apply_targeting`, :330); conversation subjects sample on `thread_id` (`_apply_conversation_targeting`, :467). |
| **No-loop guard** | `ingestion_service.py:68` (skip scheduling) + `evaluation_service.py:213` (refuse outright) | Both are needed — monitors/manual runs bypass ingest. `_REAL` in `async_reader` is the third leg (list/count/metric filtering). |
| **Idempotency** | `score_writer.py:76` | See §5. |
| **Debounce** | `infrastructure/queue/eval_debounce.py` | Fails open. |
| **Every LLM call** | `infrastructure/llm/provider.py:485/543` | `run_structured_agent` / `run_text_agent`. `llm_enabled()` (:268) gates the judge; `use_project_key()` (:248) scopes the key. |
| **Level ↔ UI vocabulary** | `evaluation_service.py:58 _LEVEL_NAME` | `CONVERSATION→conv`, `AGENT_RUN→msg`, `SPAN/TOOL/GENERATION/CHAIN→step`. |
| **Level validation** | `api/routers/evaluators.py:44-66` | `VALID_LEVELS`, `VALID_KINDS`, `STRUCTURAL_LEVELS` — rejects a run-level check stamped TOOL. |

---

## 7. Config surface (`config.py`)

| Setting | Default | Effect |
|---|---|---|
| `eval_debounce_seconds` | `4` | Countdown on both trace and thread eval tasks. |
| `eval_latency_budget_ms` | `60000` | `LatencyEvaluator` fallback budget. |
| `column_default_model` | `typesafe/jev-1.13` | Model a new column starts on; what a decision column with no model runs on. |
| `llm_judge_model` | `openai/gpt-6-luna` | Default text model: LLM columns with no model, the "Use AI" generator, attackers. |
| `gate_quality_score_name` | `tracely.run.quality` | The one judge the gate enforces (`quality_specs`, :355). Blank = every AGENT_RUN judge. |
| `gate_quality_blocks` | `true` | Whether a sub-threshold quality grade FAILs a replayed case. |
| `eval_chat_enabled` / `eval_chat_pool_size` | `true` / `8` | Durable sequential-judge conversations (LangGraph Postgres checkpointer). |

Seeded evaluator catalog: `domain/evaluation/evaluators/catalog.py` `TEMPLATES` — idempotent by
`score_name`, installed as editable rows (not hardcoded defaults).

---

## 8. The API + frontend surface

| Endpoint | Router | Purpose |
|---|---|---|
| `POST /api/evaluations/run` | `evaluations.py:54` | SSE run. Concurrency 3, 200 targets max, `data:` frames documented in the module docstring. |
| `GET/POST/PATCH/DELETE /api/evaluators[/{id}]` | `evaluators.py:150,297,322,353` | Column CRUD. |
| `GET /api/evaluators/templates` · `/models` · `/cost` | `evaluators.py:210,159,172` | Catalog, judge model list, per-column spend. |
| `GET /api/evaluators/template-variables/{level}` | `evaluators.py:223` | Advanced-mode variable discovery. |
| `POST /api/evaluators/resolve` | `evaluators.py:230` | Prompt preview — **same** `build_context` + resolver as the run path, so preview matches reality. |
| `POST /api/evaluators/generate` | `evaluators.py:277` | AI-generate a column config (`domain/evaluation/generation.py`). |

Frontend: `frontend/app/lib/evaluators.ts` (browser helpers; `streamEvaluationRun` at :212) →
Next proxies under `frontend/app/api/evaluators*` and `frontend/app/api/evaluations/run/route.ts`,
which re-issue server-side with the Bearer key.

---

## 9. Tests to read (they're the executable spec)

```
backend/tests/test_evaluation_run_stream.py   # thread loop, sequential chaining, SSE emission
backend/tests/test_llm_judge_levels.py        # per-level context assembly
backend/tests/test_eval_targeting.py          # spec_applies + deterministic sampling
backend/tests/test_eval_debounce.py           # _should_run truth table
backend/tests/test_verdict_policy.py          # advisory roll-up
backend/tests/test_catalog_verdicts.py        # seeded template verdicts
backend/tests/test_evaluators_api.py          # level/kind validation
backend/tests/test_gate_eval.py               # gate → eval wiring
backend/tests/test_introspection.py:359       # the "refuse internal traces" guard
```

`uv run pytest -q backend/tests/test_evaluation_run_stream.py` — no infra needed.

---

## 10. Debugging recipes

- **A score didn't appear.** In order: is the evaluator `enabled`? did targeting/sampling drop it
  (`_apply_targeting`)? was the eval superseded (`{"skipped": "superseded"}` in the worker log)?
  did the judge fail (`llm_judge_failed` warning) or is there no LLM key (`llm_enabled()` → `[]`)?
- **Sequential column behaving like batch.** It only chains on the thread pass. Check
  `needs_thread_pass` came back true and `evaluate_conversation_task` actually ran.
- **See what the judge saw.** Every dispatch is recorded as an internal trace — Traces tab →
  **Evals** chip. One span per column, the LLM call nested underneath with the resolved prompt.
- **Worker changes don't apply.** `docker compose restart worker` — Celery does not hot-reload.
