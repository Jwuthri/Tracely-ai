"""audits: behavioural audits (Petri/Bloom-style), their seeds, runs and rollouts.

Four tables behind the Audits feature (`domain/audit/`):

- `audits`         — the definition: behaviour, target, judges (config JSON). Hand-authored.
- `audit_seeds`    — instructions to the attacker.
- `audit_runs`     — one execution, with a config snapshot, budget and spend.
- `audit_rollouts` — one attacker ⇄ target conversation, checkpointed and leased so it can be
                     advanced in short slices by any worker.

`audits.agent_slug` is deliberately not a foreign key (same as `evaluators.target_agent`): the
audit must survive a data wipe, which deletes agents.

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), index=True),
        sa.Column("name", sa.String(200), server_default=""),
        sa.Column("agent_slug", sa.String(128), server_default=""),
        sa.Column("behavior", sa.Text(), server_default=""),
        sa.Column("config", sa.JSON(), server_default="{}"),
        sa.Column("created_by", sa.String(128), server_default="ui"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "audit_seeds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("audit_id", sa.String(36), sa.ForeignKey("audits.id"), index=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), index=True),
        sa.Column("title", sa.String(512), server_default=""),
        sa.Column("instruction", sa.Text(), server_default=""),
        sa.Column("tags", sa.JSON(), server_default="[]"),
        sa.Column("origin", sa.String(16), server_default="manual"),
        sa.Column("parent_seed_id", sa.String(36), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "audit_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("audit_id", sa.String(36), sa.ForeignKey("audits.id"), index=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), index=True),
        sa.Column("status", sa.String(16), server_default="RUNNING"),
        sa.Column("mode", sa.String(16), server_default="measure"),
        sa.Column("config", sa.JSON(), server_default="{}"),
        sa.Column("budget_usd", sa.Float(), server_default="0"),
        sa.Column("spend_usd", sa.Float(), server_default="0"),
        sa.Column("total", sa.Integer(), server_default="0"),
        sa.Column("done", sa.Integer(), server_default="0"),
        sa.Column("summary", sa.JSON(), server_default="{}"),
        sa.Column("heartbeat_at", sa.Float(), server_default="0"),
        sa.Column("created_by", sa.String(128), server_default="ui"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "audit_rollouts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("audit_runs.id"), index=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), index=True),
        sa.Column("seed_id", sa.String(36), server_default=""),
        sa.Column("seed", sa.JSON(), server_default="{}"),
        sa.Column("rep", sa.Integer(), server_default="0"),
        sa.Column("position", sa.Integer(), server_default="0"),
        sa.Column("stage", sa.Integer(), server_default="0"),
        sa.Column("phase", sa.String(16), server_default="PENDING"),
        sa.Column("verdict", sa.String(16), server_default=""),
        sa.Column("state", sa.JSON(), nullable=True),
        sa.Column("state_key", sa.String(512), server_default=""),
        sa.Column("scores", sa.JSON(), server_default="{}"),
        sa.Column("detail", sa.JSON(), server_default="{}"),
        sa.Column("spend_usd", sa.Float(), server_default="0"),
        sa.Column("attempts", sa.Integer(), server_default="0"),
        sa.Column("lease_token", sa.String(36), server_default=""),
        sa.Column("lease_expires_at", sa.Float(), server_default="0"),
        sa.Column("sandbox", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_audit_rollout_run_phase", "audit_rollouts", ["run_id", "phase"])


def downgrade() -> None:
    op.drop_index("ix_audit_rollout_run_phase", table_name="audit_rollouts")
    op.drop_table("audit_rollouts")
    op.drop_table("audit_runs")
    op.drop_table("audit_seeds")
    op.drop_table("audits")
