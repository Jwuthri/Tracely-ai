"""Postgres span store: `events`, `scores` and the per-trace rollup `traces`

The ClickHouse tables' twins, read when `SPAN_STORE=postgres`. Raw DDL with no ORM models: the
span readers own their column lists (as the ClickHouse ones do), and the SQLite test harness only
creates registry tables — jsonb/text[] would not build there. Purely additive: with the store left
on ClickHouse these stay empty. See design/part2-tracely/14-clickhouse-to-postgres.md §2-§3.

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_T = "text NOT NULL DEFAULT ''"

UPGRADE = f"""
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS events (
  project_id             text        NOT NULL,
  trace_id               text        NOT NULL,
  span_id                text        NOT NULL,
  parent_span_id         {_T},
  start_time             timestamptz NOT NULL,
  end_time               timestamptz,
  completion_start_time  timestamptz,
  name                   {_T},
  type                   {_T},
  environment            text NOT NULL DEFAULT 'default',
  env                    text NOT NULL DEFAULT 'prod',
  version                {_T},
  release                {_T},
  level                  text NOT NULL DEFAULT 'DEFAULT',
  status_message         {_T},
  is_app_root            boolean NOT NULL DEFAULT false,
  trace_name             {_T},
  user_id                {_T},
  session_id             {_T},
  tags                   text[] NOT NULL DEFAULT '{{}}',
  agent_id               {_T},
  agent_version_id       {_T},
  agent_run_id           {_T},
  conversation_id        {_T},
  turn_id                {_T},
  turn_index             integer NOT NULL DEFAULT 0,
  step_id                {_T},
  step_name              {_T},
  tool_call_id           {_T},
  caller_agent_id        {_T},
  callee_agent_id        {_T},
  edge_type              {_T},
  evaluation_case_id     {_T},
  gate_run_id            {_T},
  failure_cluster_id     {_T},
  internal_kind          {_T},
  subject_id             {_T},
  model_id               {_T},
  model_parameters       {_T},
  usage_details          jsonb NOT NULL DEFAULT '{{}}',
  cost_details           jsonb NOT NULL DEFAULT '{{}}',
  -- summed once at write time (ClickHouse did arraySum(mapValues(...)) on every read)
  tokens                 bigint NOT NULL DEFAULT 0,
  input_tokens           bigint NOT NULL DEFAULT 0,
  output_tokens          bigint NOT NULL DEFAULT 0,
  cost                   double precision NOT NULL DEFAULT 0,
  tool_definitions       jsonb NOT NULL DEFAULT '{{}}',
  tool_calls             text[] NOT NULL DEFAULT '{{}}',
  tool_call_names        text[] NOT NULL DEFAULT '{{}}',
  -- NULL = no text, never '' (events_schema.to_rows enforces it). TOASTed out of line, so a scan
  -- that does not select them never reads them.
  input                  text COMPRESSION lz4,
  output                 text COMPRESSION lz4,
  metadata               jsonb NOT NULL DEFAULT '{{}}',
  source                 text NOT NULL DEFAULT 'otel',
  service_name           {_T},
  scope_name             {_T},
  telemetry_sdk_language {_T},
  telemetry_sdk_name     {_T},
  telemetry_sdk_version  {_T},
  created_at             timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS events_conv  ON events (project_id, conversation_id) WHERE conversation_id <> '';
CREATE INDEX IF NOT EXISTS events_time  ON events (project_id, start_time);
CREATE INDEX IF NOT EXISTS events_agent ON events (project_id, agent_id);
CREATE INDEX IF NOT EXISTS events_root_input ON events (project_id, agent_id, md5(input)) WHERE is_app_root;
CREATE INDEX IF NOT EXISTS events_run_id ON events (project_id, (metadata->>'tracely.replay.run_id'))
  WHERE metadata ? 'tracely.replay.run_id';
-- Text search. A GENERATION's input is the whole history + system prompt, repeated every turn:
-- noise for search and most of the index, so only its OUTPUT is indexed.
CREATE INDEX IF NOT EXISTS events_output_trgm ON events USING gin (output gin_trgm_ops);
CREATE INDEX IF NOT EXISTS events_input_trgm  ON events USING gin (input gin_trgm_ops) WHERE type <> 'GENERATION';

CREATE TABLE IF NOT EXISTS scores (
  project_id         text NOT NULL,
  id                 text NOT NULL,
  trace_id           text,
  observation_id     text,
  session_id         text,
  agent_run_id       {_T},
  turn_id            {_T},
  name               text NOT NULL,
  source             text NOT NULL,
  data_type          {_T},
  value              double precision,
  string_value       {_T},
  verdict            {_T},
  evaluation_case_id {_T},
  gate_run_id        {_T},
  evaluation_level   {_T},
  execution_trace_id {_T},
  comment            {_T},
  metadata           jsonb NOT NULL DEFAULT '{{}}',
  -- write time; a re-grade overwrites it, exactly as FINAL surfaced the newest version
  created_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, id)
);
CREATE INDEX IF NOT EXISTS scores_trace   ON scores (project_id, trace_id);
CREATE INDEX IF NOT EXISTS scores_session ON scores (project_id, session_id) WHERE evaluation_level = 'CONVERSATION';
CREATE INDEX IF NOT EXISTS scores_name    ON scores (project_id, name, created_at);
CREATE INDEX IF NOT EXISTS scores_online_fail ON scores (project_id, trace_id, name)
  WHERE source = 'EVAL' AND evaluation_case_id = '' AND verdict = 'FAIL';

-- One row per trace, recomputed from its spans in the same transaction as every span write or
-- delete (tracely.infrastructure.spans.rollup). Lists, counts and trends read this instead of
-- grouping spans. Verdicts are deliberately NOT here: the advisory set changes independently.
CREATE TABLE IF NOT EXISTS traces (
  project_id        text NOT NULL,
  trace_id          text NOT NULL,
  thread_id         text NOT NULL,
  conversation_id   {_T},
  env               text NOT NULL DEFAULT 'prod',
  internal_kind     {_T},
  subject_id        {_T},
  agent_id          {_T},
  app_root_agent_id {_T},
  root_name         {_T},
  first_name        {_T},
  root_input       text COMPRESSION lz4,
  root_output       text COMPRESSION lz4,
  has_input         boolean NOT NULL DEFAULT false,
  has_output        boolean NOT NULL DEFAULT false,
  spans             integer NOT NULL DEFAULT 0,
  ts_min            timestamptz NOT NULL,
  ts_max            timestamptz NOT NULL,
  latency_ms        bigint NOT NULL DEFAULT 0,
  tokens            bigint NOT NULL DEFAULT 0,
  input_tokens      bigint NOT NULL DEFAULT 0,
  output_tokens     bigint NOT NULL DEFAULT 0,
  cost              double precision NOT NULL DEFAULT 0,
  model_id          {_T},
  has_error         boolean NOT NULL DEFAULT false,
  tool_errors       integer NOT NULL DEFAULT 0,
  first_error       {_T},
  has_emulated_turn boolean NOT NULL DEFAULT false,
  metadata          jsonb NOT NULL DEFAULT '{{}}',
  updated_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, trace_id)
);
CREATE INDEX IF NOT EXISTS traces_thread ON traces (project_id, thread_id);
CREATE INDEX IF NOT EXISTS traces_recent ON traces (project_id, ts_max DESC);
CREATE INDEX IF NOT EXISTS traces_start  ON traces (project_id, ts_min);
CREATE INDEX IF NOT EXISTS traces_agent  ON traces (project_id, agent_id);
CREATE INDEX IF NOT EXISTS traces_root_input_trgm ON traces USING gin (root_input gin_trgm_ops);
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS traces; DROP TABLE IF EXISTS scores; DROP TABLE IF EXISTS events;"
    )
