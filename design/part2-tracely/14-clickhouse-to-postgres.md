# 14 — Moving `events` / `scores` from ClickHouse to Postgres

**Status:** plan, not started · **Date:** 2026-11-15 · **Supersedes:** the ClickHouse half of
[`09-database-schema.md`](09-database-schema.md) and the "ClickHouse `events`" box in
[`02-system-architecture.md`](02-system-architecture.md).

**Decision.** Tracely stores spans and scores in the Postgres it already runs. ClickHouse, its
DDL runner, its config and its maintenance jobs are deleted. This leaves four stores: Postgres,
S3/MinIO (raw OTLP stays the source of truth), Redis and pgvector (which lives in the same Postgres).

---

## 1. Why

What prod looked like on 2026-11-15:

| | |
|---|---|
| Live spans | **66.5k** (3 projects, 18.5k traces, 4.9k conversations) |
| Physical rows in `events` | 612k. **89% are lightweight-deleted rows** that were never compacted. Every query that reads `input` scans them. |
| Prompt text | 630 MiB live (9.3 GiB including dead rows), GENERATION input averages 30 KB |
| Read mix (24 days, 207k SELECTs) | **93% are by-trace span reads.** The threads list read 9 GiB per call and took 6–300 s. |

Benchmark: the real `async_reader` functions on ClickHouse 24.8 against hand-ported Postgres 17,
using synthetic data shaped like prod, with 2 CPU / 3 GB per container. Warm median times:

| Query | 1×: CH | 1×: PG | 10×: CH | 10×: PG (rollup) |
|---|---|---|---|---|
| Open a trace | 12.9 ms | **0.5 ms** | 24 ms | **1.0 ms** |
| Threads page | 72 ms | 85 ms | 427 ms | **400 ms** |
| Threads count | 29 ms | 29 ms | 167 ms | **120 ms** |
| Dashboard stats | 24 ms | 43 ms | 105 ms | **34 ms** |
| Ops trends, 30 days | 36 ms | 65 ms | **150 ms** | 744 ms (span table, not re-run on the rollup) |
| Text search (trigram, root + non-GENERATION spans) | 214 ms | **3 ms** | 2.7–4.3 s | **3–16 ms** |

ClickHouse is about 5× smaller on disk, and wins a brute-force scan over all text (3.9 s vs 88 s
without an index). Neither matters at 630 MiB. Every other number favours Postgres, or ties once the
per-trace rollup (§3) exists. The larger cost has been operations: hand-written PREWHERE, FINAL on
every read, tombstones instead of mutations, mark-cache ghosts, thread-pool jemalloc bloat, a
crash-loop from `background_pool_size`, 8 GB of unbounded `system.*` logs, lightweight deletes that
never leave the disk, and `MEMORY_LIMIT_EXCEEDED` on eks-dev. Self-hosting (Coolify, EKS) means
running one more stateful service.

ClickHouse has no row index, so a query either matches the sort key and is fast, or it
decompresses whole columns and is slow. Postgres's B-tree plus TOAST (large text stored out of
line, read only for rows that are returned) matches this workload: point lookups, lists of small
rows, and one trace's large text.

---

## 2. Schema (Alembic `0040_span_store`)

Raw DDL through `op.execute`, with **no ORM models**. The readers own their column lists, as they
do today. The SQLite test harness (`backend/tests/conftest.py`) creates only the registry tables,
and `jsonb`/`text[]` would break it there.

> Rebase on `origin/master` first: it has `0039_project_assistant_model` and the CH
> `0006_events_has_text` DDL. Postgres does not need 0006, because `input IS NOT NULL` reads no text.

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- contrib: present in pgvector/pgvector and timescaledb-ha

CREATE TABLE events (
  project_id            text        NOT NULL,
  trace_id              text        NOT NULL,
  span_id               text        NOT NULL,
  parent_span_id        text        NOT NULL DEFAULT '',
  start_time            timestamptz NOT NULL,
  end_time              timestamptz,
  completion_start_time timestamptz,
  name                  text        NOT NULL,
  type                  text        NOT NULL,
  environment           text        NOT NULL DEFAULT 'default',
  env                   text        NOT NULL DEFAULT 'prod',
  version               text        NOT NULL DEFAULT '',
  release               text        NOT NULL DEFAULT '',
  level                 text        NOT NULL DEFAULT 'DEFAULT',
  status_message        text        NOT NULL DEFAULT '',
  is_app_root           boolean     NOT NULL DEFAULT false,
  trace_name            text        NOT NULL DEFAULT '',
  user_id               text        NOT NULL DEFAULT '',
  session_id            text        NOT NULL DEFAULT '',
  tags                  text[]      NOT NULL DEFAULT '{}',
  agent_id              text        NOT NULL DEFAULT '',
  agent_version_id      text        NOT NULL DEFAULT '',
  agent_run_id          text        NOT NULL DEFAULT '',
  conversation_id       text        NOT NULL DEFAULT '',
  turn_id               text        NOT NULL DEFAULT '',
  turn_index            integer     NOT NULL DEFAULT 0,
  step_id               text        NOT NULL DEFAULT '',
  step_name             text        NOT NULL DEFAULT '',
  tool_call_id          text        NOT NULL DEFAULT '',
  caller_agent_id       text        NOT NULL DEFAULT '',
  callee_agent_id       text        NOT NULL DEFAULT '',
  edge_type             text        NOT NULL DEFAULT '',
  evaluation_case_id    text        NOT NULL DEFAULT '',
  gate_run_id           text        NOT NULL DEFAULT '',
  failure_cluster_id    text        NOT NULL DEFAULT '',
  internal_kind         text        NOT NULL DEFAULT '',
  subject_id            text        NOT NULL DEFAULT '',
  model_id              text        NOT NULL DEFAULT '',
  model_parameters      text        NOT NULL DEFAULT '',
  usage_details         jsonb       NOT NULL DEFAULT '{}',
  cost_details          jsonb       NOT NULL DEFAULT '{}',
  -- computed once in events_schema.to_rows (CH did arraySum(mapValues(...)) on every read)
  tokens                bigint      NOT NULL DEFAULT 0,
  input_tokens          bigint      NOT NULL DEFAULT 0,
  output_tokens         bigint      NOT NULL DEFAULT 0,
  cost                  double precision NOT NULL DEFAULT 0,
  tool_definitions      jsonb       NOT NULL DEFAULT '{}',
  tool_calls            text[]      NOT NULL DEFAULT '{}',
  tool_call_names       text[]      NOT NULL DEFAULT '{}',
  input                 text        COMPRESSION lz4,   -- NULL = no text (to_rows already enforces '' -> NULL)
  output                text        COMPRESSION lz4,
  metadata              jsonb       NOT NULL DEFAULT '{}',
  source                text        NOT NULL DEFAULT 'otel',
  service_name          text        NOT NULL DEFAULT '',
  scope_name            text        NOT NULL DEFAULT '',
  telemetry_sdk_language text       NOT NULL DEFAULT '',
  telemetry_sdk_name    text        NOT NULL DEFAULT '',
  telemetry_sdk_version text        NOT NULL DEFAULT '',
  created_at            timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, trace_id, span_id)          -- by-trace reads, span_count, dedup key
);
CREATE INDEX events_conv  ON events (project_id, conversation_id) WHERE conversation_id <> '';
CREATE INDEX events_time  ON events (project_id, start_time);          -- ops windows, retention
CREATE INDEX events_agent ON events (project_id, agent_id);            -- gate candidates, versions
CREATE INDEX events_root_input ON events (project_id, agent_id, md5(input)) WHERE is_app_root;  -- traces_with_input
CREATE INDEX events_run_id ON events (project_id, (metadata->>'tracely.replay.run_id'))
  WHERE metadata ? 'tracely.replay.run_id';                            -- traces_for_run
-- Text search. GENERATION input is the whole history plus the system prompt, repeated on every
-- turn, so it is noise for search and would be most of the index.
CREATE INDEX events_output_trgm ON events USING gin (output gin_trgm_ops);
CREATE INDEX events_input_trgm  ON events USING gin (input gin_trgm_ops) WHERE type <> 'GENERATION';

CREATE TABLE scores (
  project_id         text NOT NULL,
  id                 text NOT NULL,              -- uuid5, deterministic (score_writer.eval_score_id)
  trace_id           text,
  observation_id     text,
  session_id         text,
  agent_run_id       text NOT NULL DEFAULT '',
  turn_id            text NOT NULL DEFAULT '',
  name               text NOT NULL,
  source             text NOT NULL,
  data_type          text NOT NULL,
  value              double precision,
  string_value       text NOT NULL DEFAULT '',
  verdict            text NOT NULL DEFAULT '',
  evaluation_case_id text NOT NULL DEFAULT '',
  gate_run_id        text NOT NULL DEFAULT '',
  evaluation_level   text NOT NULL DEFAULT '',
  execution_trace_id text NOT NULL DEFAULT '',
  comment            text NOT NULL DEFAULT '',
  metadata           jsonb NOT NULL DEFAULT '{}',
  created_at         timestamptz NOT NULL DEFAULT now(),   -- write time; re-grade overwrites it, as FINAL did
  PRIMARY KEY (project_id, id)
);
CREATE INDEX scores_trace   ON scores (project_id, trace_id);
CREATE INDEX scores_session ON scores (project_id, session_id) WHERE evaluation_level = 'CONVERSATION';
CREATE INDEX scores_name    ON scores (project_id, name, created_at);
CREATE INDEX scores_online_fail ON scores (project_id, trace_id, name)
  WHERE source = 'EVAL' AND evaluation_case_id = '' AND verdict = 'FAIL';   -- every "failing" subquery
```

No FINAL, `is_deleted`, `event_ts`, `SAMPLE BY` or TTL. The CH bloom filters on `agent_run_id`,
`gate_run_id`, `evaluation_case_id` and `subject_id` are not carried over, because no query filters
`events` on them.

---

## 3. The per-trace rollup: `traces`

At 10× the thread list was 1.06 s when grouped from spans and 0.40 s from this table. Each list,
count and trend reads one row per trace instead of 3.6 spans.

```sql
CREATE TABLE traces (
  project_id        text NOT NULL,
  trace_id          text NOT NULL,
  thread_id         text NOT NULL,      -- coalesce(nullif(max(conversation_id), ''), trace_id)
  conversation_id   text NOT NULL DEFAULT '',
  env               text NOT NULL DEFAULT 'prod',
  internal_kind     text NOT NULL DEFAULT '',   -- _REAL lives here too
  subject_id        text NOT NULL DEFAULT '',
  agent_id          text NOT NULL DEFAULT '',   -- the _TRACE_AGENT rule (root, else app-root, else any)
  app_root_agent_id text NOT NULL DEFAULT '',   -- anyIf(agent_id, is_app_root): trace_agent_ids
  root_name         text NOT NULL DEFAULT '',   -- _root("name"), else earliest span's name
  root_input        text COMPRESSION lz4,       -- _root("input"): what every judge grades
  root_output       text COMPRESSION lz4,
  has_input         boolean NOT NULL DEFAULT false,   -- _root_has, NULL map semantics
  has_output        boolean NOT NULL DEFAULT false,
  spans             integer NOT NULL,
  ts_min            timestamptz NOT NULL,
  ts_max            timestamptz NOT NULL,       -- max(coalesce(end_time, start_time))
  latency_ms        bigint NOT NULL,
  tokens            bigint NOT NULL DEFAULT 0,
  input_tokens      bigint NOT NULL DEFAULT 0,
  output_tokens     bigint NOT NULL DEFAULT 0,
  cost              double precision NOT NULL DEFAULT 0,
  model_id          text NOT NULL DEFAULT '',   -- argMaxIf(model_id, tokens, GENERATION)
  has_error         boolean NOT NULL DEFAULT false,
  tool_errors       integer NOT NULL DEFAULT 0,
  first_error       text NOT NULL DEFAULT '',   -- earliest non-empty status_message (member_meta)
  has_emulated_turn boolean NOT NULL DEFAULT false,   -- latest_traces_for_env excludes these
  metadata          jsonb NOT NULL DEFAULT '{}',      -- merged tracely.metadata.* keys
  updated_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, trace_id)
);
CREATE INDEX traces_thread ON traces (project_id, thread_id);
CREATE INDEX traces_recent ON traces (project_id, ts_max DESC);
CREATE INDEX traces_start  ON traces (project_id, ts_min);
CREATE INDEX traces_agent  ON traces (project_id, agent_id);
CREATE INDEX traces_root_input_trgm ON traces USING gin (root_input gin_trgm_ops);   -- search_threads
```

**Write rule.** `rollup.refresh(conn, project_id, trace_ids)` is a single
`INSERT INTO traces SELECT … FROM events WHERE project_id = $1 AND trace_id = ANY($2) GROUP BY trace_id
ON CONFLICT (project_id, trace_id) DO UPDATE SET …`, followed by
`DELETE FROM traces t WHERE … AND NOT EXISTS (spans)`. It recomputes from the spans every time,
so late spans, re-deliveries and partial deletes all converge, the same way ReplacingMergeTree did.

It is called **in the same transaction** as every span write or delete:

1. `IngestionService.process_blob` (`services/ingestion_service.py:71`), which is the one span
   insert. The inline emulated-turn ingest and introspection recordings reach the same writer.
2. `deletes.delete_trace`, `delete_threads`, and retention.

**Concurrency.** Two workers can ingest spans of the same trace at once. Under READ COMMITTED,
each recompute would miss the other's uncommitted spans. Before the insert, take
`pg_advisory_xact_lock(hashtextextended(project_id || ':' || trace_id, 0))` for each touched trace,
in sorted order so there are no deadlocks. Today this costs nothing: the worker runs
`--pool=solo --concurrency=1`. It matters as soon as `CELERY_CONCURRENCY` is above 1.

**What the rollup does not contain: verdicts.** Scores change separately, and the advisory set can
change at any time. The "failing" flag stays a read-time join against `scores_online_fail` with
`name <> ALL(adv)`. That keeps the one verdict policy in one SQL expression (§7).

---

## 4. Read mapping

New modules live under `backend/tracely/infrastructure/spans/`: `reads.py` (async, for the API),
`trace_reader.py` (sync `TraceReader`, for workers), `writer.py`, `rollup.py`, `deletes.py`.
**Function names, signatures and return shapes do not change**, so routers, services and the
frontend are untouched. Async goes through `db/engine.async_engine` (asyncpg) and sync through
`db/engine.sync_engine` (psycopg), both with `text()` and bound parameters.

### `async_reader.py` → `spans/reads.py`

| Function | Reads | Notes |
|---|---|---|
| `traces_overview` | `traces` + fail join | `ORDER BY ts_min DESC LIMIT n` |
| `trace_spans` | `events` PK | was PREWHERE; now an index range |
| `thread_spans_full` | `events` PK ∪ `events_conv` | `conversation_id = $th OR trace_id = $th` → BitmapOr |
| `thread_turn_count` | `traces_thread` | `count(*) WHERE thread_id = $th AND internal_kind = ''` |
| `trace_scores` | `scores_trace` ∪ `scores_session` | |
| `evaluator_cost` | `scores_name` | `metadata->>'eval.total_tokens'` with `jsonb ?` instead of `mapContains` |
| `traces_in_window` | `traces_start` | `env <> 'ci' AND internal_kind = ''` |
| `score_samples_in_window` | `scores_name` (+ `traces` join for an agent) | join on `traces.agent_id`, not span `agent_id`: see §5 |
| `trace_failure_samples_in_window` | `traces` + fail join | `root_agent` is `traces.agent_id` |
| `evaluator_catalog`, `evaluator_score_queue` | `scores` + `EXISTS traces` (`_REVIEWABLE`) | `ORDER BY md5(id)` replaces `cityHash64(id)` |
| `sessions_overview` (page / `count_only`) | `traces` grouped by `thread_id` + fail join | page text comes from `traces.root_input/root_output`, so `_trace_texts` is no longer a separate query |
| `sessions_overview(q=…)` | thread rollup ∪ trigram hit on `events` | `text_hit` = `EXISTS (events … AND (output ILIKE q OR (type <> 'GENERATION' AND input ILIKE q)))` per thread, plus the haystack (`model`, `metadata::text`, `agent_id`, `root_name`, `subject_id`) |
| `thread_agents` | — | Python over `thread_spans_full` |
| `session_turns` | `traces` + fail join | `WHERE thread_id = $th`; the "other conversation_id" guard is the key itself |
| `scores_by_trace`, `conversation_scores_by_thread` | `scores_trace`, `scores_session` | `trace_id = ANY($ids)` |
| `agent_trace_ids` | `traces_agent` / `traces_recent` | |
| `trace_agent_ids` | `traces` | `DISTINCT app_root_agent_id` |
| `agent_ids_with_spans` | `events_agent` | `DISTINCT agent_id` including internal, as today |
| `_trace_ops_rows` | `traces` | `latency_ms`, `tokens`, `tool_errors` |
| `agent_score_rows` | — | composition, no SQL of its own |
| `search_threads` | `traces_root_input_trgm` | matches `root_input`: see §5 |
| `stats_counts` | `traces` + `scores_online_fail` | spans = `sum(spans)` |
| `ops_metrics` daily | `traces_start` | `percentile_cont(...) WITHIN GROUP`, `GROUP BY ROLLUP (d)` (same ROLLUP trick) |
| `ops_metrics` per model / slowest | `events_time` | still span-level; 0.5–0.7 s at 10×, see §10 |
| `daily_trace_failures`, `trace_failure_totals` | `traces` + fail join | |

### `trace_reader.py` (sync) → `spans/trace_reader.py`

| Method | Reads | Notes |
|---|---|---|
| `read_spans`, `read_thread_spans` | `events` PK / `events_conv` | the eval runner, regression, gate and FI keep their `_SPAN_COLS` |
| `thread_trace_ids` | `traces_thread` | `ORDER BY ts_min` |
| `candidate_metrics` | `traces` | `latency_ms`, `tokens` |
| `latest_traces_for_env` | `traces` | `agent_id`, `env`, `internal_kind = ''`, `NOT has_emulated_turn`: see §5 |
| `traces_with_input` | `events_root_input` | `md5(input) = md5($i) AND input = $i` |
| `traces_for_run` | `events_run_id` | |
| `failing_trace_reasons`, `scores_by_trace` | `scores_online_fail`, `scores_trace` | |
| `recent_failing_traces` | `scores_online_fail` ⋈ `traces` | input = `root_input`: see §5 |
| `span_count` | `events` PK | index-only count; it is a stability signal, so it reads spans, not the rollup |
| `member_timestamps` | `traces` | `ts_min` |
| `version_breakdown` | `events_agent` | per-trace `anyIf(agent_version_id)` stays span-level |
| `member_meta` | `traces` | `ts_min`, `latency_ms`, `root_input`, `first_error` |

### Raw ClickHouse SQL outside the two readers (move it into them)

| Where | Today | Postgres |
|---|---|---|
| `services/ingestion_service.py:119-127` (`_fill_agents_workflow_root`) | `SELECT input, output, start_time FROM events FINAL PREWHERE trace_id …` on its own client | new `TraceReader.trace_generations(project_id, trace_id)` on `events` PK. This also removes a second "SQL outside the readers" exception. |
| `services/selfcheck_service.py:74-92` | `max(start_time) FROM events` + `system.disks` | `max(ts_max) FROM traces`. Postgres cannot see its filesystem's free space from SQL. Report `pg_database_size()` against an optional `DB_VOLUME_GB` setting, and skip the disk verdict when it is unset (it is already skipped when CH is unreachable). **Open decision.** |
| `api/routers/health.py:52-56` | `deps["clickhouse"]` = async client `ping()` | drop the dependency. Postgres is already probed. |

Translation idioms used throughout: `countIf(x)` → `count(*) FILTER (WHERE x)`;
`anyIf(c, x)` → `max(c) FILTER (WHERE x)`; `argMin(a, b)` → `(array_agg(a ORDER BY b))[1]`;
`uniqExact(x)` → `count(DISTINCT x)`; `positionCaseInsensitiveUTF8(h, q) > 0` → `h ILIKE '%' || q || '%'`
(escape `%`/`_` in `q`); `x IN {a:Array(String)}` → `x = ANY($a)`;
`subtractDays(now(), d)` → `now() - make_interval(days => $d)`.

---

## 5. Deliberate behaviour changes

Each needs a line in the release notes and a test that pins the new rule.

1. **Text search skips GENERATION *inputs*.** They hold the whole history and the system prompt,
   so any word in a system prompt matched every thread. Root input, tool I/O and every output
   (assistant replies included) are still searched.
2. **"First input" means the root input** (the `_root` rule) in `search_threads`,
   `recent_failing_traces` and `member_meta`. It used to be the earliest non-empty input of any
   span. The two agree whenever the root carries the user message. Otherwise the old rule returned
   a GENERATION's full history.
3. **Agent filters use the trace's agent** (`traces.agent_id`, the `_TRACE_AGENT` rule) in
   `latest_traces_for_env` and `score_samples_in_window`. They used to use "any span with this
   agent", which filed a sub-agent's spans under the parent's traces. `version_breakdown` keeps the
   span-level rule, because a version is stamped on the agent's own span.
4. **The calibration queue order changes** (`md5(id)` instead of `cityHash64(id)`). It is still a
   stable random sample. Existing labels are keyed by score id and are unaffected.
5. **Retention removes whole traces** once a trace's newest span is past the cutoff. A per-span
   TTL used to cut a long-running trace in half.
6. **Dedup key is `(project_id, trace_id, span_id)`.** CH also keyed on the exact `start_time`, so a
   re-delivered span with a shifted clock duplicated. Now it replaces.
7. **Deletes are immediate.** No tombstone window, and no "masked but still read" rows.

---

## 6. Writes and deletes

| Today (ClickHouse) | Postgres |
|---|---|
| `client.insert_rows` + `async_insert` | `writer.upsert_events`: dedupe the batch by key in Python (ON CONFLICT cannot touch a row twice in one statement), `executemany` `INSERT … ON CONFLICT (project_id, trace_id, span_id) DO UPDATE SET <all columns> = EXCLUDED.*`, then `rollup.refresh`, in one transaction |
| `events_schema.to_rows` | same column list plus the computed `tokens / input_tokens / output_tokens / cost`; Maps become dicts (jsonb), Arrays become lists |
| `ScoreWriter.write_eval_scores` | `INSERT … ON CONFLICT (project_id, id) DO UPDATE` (same uuid5 ids) |
| `retract_eval_scores`, `retract_evaluator` (tombstones) | `DELETE FROM scores WHERE project_id = $1 AND id = ANY($2)` |
| `write_regression_verdict` | plain INSERT (uuid4) |
| `deletes.delete_trace(step_names)` (tombstones) | `DELETE FROM events WHERE … AND step_name = ANY($s)` + `rollup.refresh` |
| `delete_threads` | delete `events` / `scores` / `traces` by trace ids, in one transaction |
| `delete_project_events`, `delete_project_rows` | `DELETE … WHERE project_id = $1` in 10k-row batches (`ctid IN (SELECT … LIMIT 10000)`), still queued through `tracely.purge_project` |
| CH TTL 90 days + `delete_expired` (plan retention) | one beat task `expire_traces`: `SELECT trace_id FROM traces WHERE project_id = ANY($p) AND ts_max < $cutoff LIMIT 1000`, then delete those traces' events, scores and rollup, looping. This replaces both the TTL and the per-plan path. It **must run on every deployment** with the 90-day cutoff, which the TTL used to give self-hosters for free. Plan retention (hosted only, today's `enforce_retention`) only narrows the cutoff for each project. |
| `last_write_by_project` (orphan sweep) | `SELECT project_id, max(updated_at) FROM traces GROUP BY project_id` |
| `maintenance.compact_tables`, `cap_system_logs` | deleted. Autovacuum handles dead tuples. |

---

## 7. Hard rules (CLAUDE.md): how each survives

- **No SQL in routers.** Unchanged in spirit. The rule's module names change from
  `clickhouse/async_reader.py` / `trace_reader.py` to `spans/reads.py` / `spans/trace_reader.py`.
- **One verdict policy.** The SQL twin becomes `name <> ALL(:adv)`. Do not use `NOT IN`, which
  cannot bind an array. `<> ALL('{}')` is true, so an empty advisory list excludes nothing,
  exactly like CH `NOT IN []`. Defined once (`_FAILING` / `_FAILING_THREADS` equivalents in
  `spans/reads.py`), as now.
- **Internal runs are never evaluated.** The ingest-hop skip and the `evaluate_trace` refusal are
  Python and keep working (`read_spans` keeps its signature). `_REAL` becomes
  `internal_kind = ''` on `traces` for every list, count and metric, and on `events` for the
  span-level ones. `sessions_overview(include_internal=True)` is still the only opt-in.
- **Writes are idempotent.** Deterministic ids are unchanged. `ON CONFLICT DO UPDATE` replaces
  ReplacingMergeTree + FINAL. The rollup is recomputed from spans, never incremented.
- **Scoped by `project_id`.** Every PK and index starts with it. Every query binds it.
- **SKIP scores dropped before roll-up, emulated-conversation invariants.** Untouched (domain
  code). The inline turn ingest must call `writer.upsert_events`, which it does if it goes through
  `IngestionService`. Verify this in PR 2.
- **"Five stores" becomes four.** Remove the ClickHouse gotchas. The "worker does not hot-reload"
  note stays.

---

## 8. Rollout

There is **no dual-write**. The ingest path writes OTLP to S3 *before* enqueueing, so a stopped
worker loses nothing: requests keep landing in S3/Redis and are ingested when the worker comes
back. A short cutover window is lossless, and dual-write would be code deleted two weeks later.

| # | PR | Contents | Risk |
|---|---|---|---|
| 1 | **Schema** | Alembic `0040_span_store` (§2, §3). Purely additive. | none |
| 2 | **Writer** | `spans/writer.py`, `rollup.py`, `deletes.py`, `expire_traces` beat task, `to_rows` computed columns. Writes go to Postgres when `SPAN_STORE=postgres` (default `clickhouse`). Wire into `docker-compose.yml`'s `x-app-env`. | low (flag off) |
| 3 | **Readers** | `spans/reads.py`, `spans/trace_reader.py` with identical signatures. `tracely.infrastructure.spans` exposes `async_reader` / `TraceReader` / `ScoreWriter` / `deletes` chosen by `SPAN_STORE` (about 10 lines). Callers switch imports to it (mechanical). **Parity tests** load one fixture set into both stores and compare outputs. | low (flag off) |
| 4 | **Copy + verify** | `scripts/copy_clickhouse_to_postgres.py`: pages CH `FINAL WHERE is_deleted = 0` by `(project_id, start_time)` in 5k-row batches through `writer.upsert_events`, then scores. Idempotent, safe to re-run. `--verify` compares per-project span/trace/score counts and runs every read in §4 against both stores for N sample threads. | none |
| — | **Cutover (ops)** | Stop the worker. Set `SPAN_STORE=postgres` on api + worker. Run the copy for all projects (66.5k spans, about a minute). Run `--verify`. Start the worker; it drains the queued ingest into Postgres. Watch `/health/queue` and the traces tab. **Rollback:** set `SPAN_STORE=clickhouse` (CH is untouched; traces ingested after cutover replay from S3). | low |
| 5 | **Remove ClickHouse** | after about a week on Postgres: delete `infrastructure/clickhouse/`, the CH DDL, the flag, `maintenance.py`, CH tests, `clickhouse-connect` (moves to a `clickhouse` extra for the copy script only), CH services in every deploy target, docs (§9). | low |
| 6 | (later) | delete the copy script and the extra after two releases | none |

**Self-hosters** (compose, Coolify, EKS) follow the same steps. The release notes for PR 4 give
the commands. PR 5's notes say: if you skipped the copy, your data is still in ClickHouse; run
`uv run --extra clickhouse python scripts/copy_clickhouse_to_postgres.py` with the old `CLICKHOUSE_*`
variables before removing the volume.

**Before any of this:** the prod worker has been stuck since 2026-11-08 04:02 UTC (41,712 queued).
Fix it first, or the cutover's "drain the queue" step means 41k tasks at once.

---

## 9. What gets deleted in PR 5

Inventory taken 2026-11-15 from local `master` plus `origin/master` (`b9dbe1d`).

**Code.** Delete:

- `backend/tracely/infrastructure/clickhouse/`: `client.py`, `async_reader.py`, `trace_reader.py`,
  `score_writer.py`, `deletes.py`, `events_schema.py` (its column list moves to `spans/`),
  `maintenance.py`, `migrations.py`, and `ddl/0001-0006`.
- Beat entries in `infrastructure/queue/celery_app.py:57-78`, and the tasks in `workers/tasks.py`
  (about lines 326-391): `cap_system_logs`, `compact_tables`.
- In `services/purge_service.py`, its call to `compact_tables`.

Two beat entries stay but get new bodies: `enforce_retention`, which now calls `expire_traces`,
and `purge_orphans`, which now calls `last_write_by_project` on `traces`.

`api/main.py:40,108` (the `close_async_client` lifespan hook) is deleted. The engines already
dispose themselves.

**Callers whose import line changes** (mechanical, no logic change):

- Routers: `traces`, `sessions`, `share`, `search`, `analytics`, `cases`, `scenarios`,
  `evaluations`, `evaluators`, `calibration`, `meta_analysis`, `admin`, `auth`, `clusters`.
  `gate` is indirect, through `gate_svc.trace_reader`.
- Services: `ingestion`, `evaluation`, `gate`, `regression`, `failure_intel`, `rolling_summary`,
  `alert_events`, `monitoring`, `purge`, `retention`, `introspection`, `selfcheck`.
  `simulation_service` reaches the writer through `IngestionService.process_blob`, which is what
  keeps the inline-ingest invariant.
- Scripts: `debug_evaluation.py`, `debug_failure_clustering.py`.

**Scripts.**

| Action | Script |
|---|---|
| delete | `scripts/ch_usage.py` |
| rewrite the store section for Postgres (`pg_total_relation_size` per table + TOAST) | `scripts/storage_report.py:74-150,321-334` |
| replace the `curl localhost:8123 TRUNCATE` | `scripts/run_all_examples.sh:45-46` |
| drop the CH migration step | `scripts/docker-start.sh:11-17` |

**Tests.**

| Action | Tests |
|---|---|
| delete | `test_ch_read_invariants.py`, `test_ch_maintenance.py`, `test_ch_migrations_wait.py` |
| rewrite for the PG store | `test_calibration_queue.py` (its tombstone case becomes a DELETE case), `test_eval_prompt.py` (`delete_trace`), `test_otel_mapping.py` (`to_rows` + computed tokens/cost), `test_purge_service.py` (no compaction, aware timestamps), `test_health.py` (`_FakeCH`), `test_agents_workflow_root.py` (`get_client` patch → reader method) |
| change only the patched import path | `test_session_filters.py`, `test_conversation_agents.py`, `test_evaluators_api.py`, `test_llm_judge_levels.py`, `test_evaluation_run_stream.py`, `test_bulk_export.py`, `test_mcp_server.py`, `test_sessions_delete.py`, `test_data_delete.py`, `test_workspace_delete.py`, `test_retention.py`, `test_scenario_hardening.py`, `test_case_grading_parity.py`, `test_case_artifacts.py`, `test_gate_eval.py`, `test_rolling_summary.py`, `test_simulation.py` |
| keep | `test_net_guard.py` (`clickhouse:8123` is still a private host worth blocking until the service is gone) |

`session_filter_clauses` is used by tests directly. Keep the function and its name, and return
PG HAVING fragments.

**Config and env.**

- `config.py:20-26` (`clickhouse_*`), `:306` (`ch_system_log_ttl_days`) and the `:109` comment.
- `.env.example:8-16,30` and `deploy/railway/.env.railway.example:19-25`.
- The `x-app-env` anchor in `docker-compose.yml:10-18` and `docker-compose.coolify.yml:14-19,32`.

`SPAN_STORE` exists in PRs 2–4 only.

**Deploy.**

- `docker-compose.yml`: the `clickhouse` service (`:146-159`), `.data/clickhouse`, the migrate
  command (`:228`) and `depends_on` (`:224,269`).
- `docker-compose.coolify.yml`: service `:57-69`, migrate `:113-126`, volume `:193`.
- `deploy/coolify/setup.py:176-186,253-255`.
- `deploy/eks/base/tracely.yaml`: ConfigMap `:16-20`, Service + StatefulSet `:73-100`, migrate Job.
- `deploy/eks/create-secret.sh:14`.
- `deploy/railway/backend.json:8` (preDeploy runs CH migrations first). Then the Railway
  `ClickHouse` service itself, deleted after the copy is verified and a final backup is taken.
- `Makefile:6,19-22` (`migrate-ch`).

**Dependency.** `backend/pyproject.toml:14` `clickhouse-connect` moves to an optional `clickhouse`
extra (for the copy script), then is deleted. Regenerate `uv.lock`, since CI runs `uv sync --frozen`.

**Frontend.** `frontend/app/components/PipelinePeek.tsx:34,51,66,243` has hard-coded "clickhouse" labels.

**Docs.**

| File | Sections |
|---|---|
| `CLAUDE.md` | Commands, Architecture, Hard rules (§7) |
| `backend/README.md` | five stores, ingest, eval, meta-analysis, module map, API surface, data schemas, key decisions |
| `README.md` | Docker, local dev, deploy, architecture, repo map |
| `CONTRIBUTING.md` | |
| `guides/OVERVIEW.md` | |
| `guides/DEPLOY.md` | env, beat, restore test, health probe |
| `guides/EVALUATION_GUIDE.md` | §6 |
| `guides/EVALUATION_CODE_MAP.md` | §2, §5 |
| `docs/pages/index.mdx` | |
| `docs/pages/self-hosting.mdx` | "Add the four databases" becomes three |
| `scripts/README.md` | |
| `workers/README.md` | |
| `deploy/railway/README.md` | |
| `deploy/eks/README.md` | |
| `deploy/coolify/README.md` | |
| `design/part2-tracely/02-system-architecture.md`, `09-database-schema.md` | point to this doc |

---

## 10. Tests and CI

- **The local suite stays infra-free.** The new Postgres tests skip unless `TRACELY_TEST_PG_URL`
  is set.
- **CI adds a Postgres service** to `.github/workflows/ci.yml`
  (`services: postgres: image: pgvector/pgvector:pg17`) and sets the URL, so the reader, rollup
  and parity tests run on every PR.
- **New invariant tests** replace `test_ch_read_invariants.py`:
  - Every reader binds `project_id`.
  - Every list, count or metric filters `internal_kind = ''`.
  - The verdict twin uses `<> ALL`.
  - `rollup.refresh` is called in the same transaction as every span write or delete.
  - A recomputed rollup equals the span-level aggregation for random fixtures.

## 11. Size

| PR | Lines |
|---|---|
| 1 | about 150 |
| 2 | about 450 |
| 3 | about 1,400 readers + about 400 tests |
| 4 | about 200 |
| 5 | about −2,400 Python (the CH package), plus DDL, maintenance, CH tests, compose/deploy blocks and the Railway CH service |

The net result is **smaller** code and one fewer stateful service everywhere. Estimated effort
is about one focused week.

## 12. Ceilings: when to revisit

| What | Measured | Upgrade path |
|---|---|---|
| Thread list from `traces` | 0.40 s at 200k traces | about 4 s at 2M traces: add a `threads` rollup refreshed the same way |
| `ops_metrics` per-model / slowest | 0.5–0.7 s at 625k spans | partial index `WHERE model_id <> ''`, or a daily per-model rollup |
| Text storage | GENERATION input repeats the history, so it grows quadratically per conversation | store only the new messages and rebuild history on read, or keep large inputs in the S3 blob only |
| Insert throughput | `executemany` | COPY into a temp table + `INSERT … SELECT … ON CONFLICT` |
| Scale | — | revisit a columnar store (or Timescale compression) past about 50M spans, or when rollup reads pass 1 s p95 |

## 13. Rejected alternatives

- **TimescaleDB hypertables.** Only Railway runs the Timescale image; compose, Coolify and EKS
  run `pgvector/pgvector`. Per-plan retention is per project, so dropping a chunk can't implement it.
- **Declarative partitioning.** It needs partition-management code, and deletes are per project anyway.
- **Triggers maintaining `traces`.** That is a hidden cost on every span. An explicit
  recompute in the ingest transaction is visible and testable.
- **Dual-write.** S3-first ingest already makes a cutover window lossless (§8).
- **Keeping ClickHouse for analytics only.** That means two span stores to keep consistent, and
  no query at this scale needs it.
