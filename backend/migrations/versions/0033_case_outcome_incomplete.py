"""INCOMPLETE case outcomes (W2): widen verdict/status columns, count incomplete cases per gate

`case_replays.verdict` was String(8) and `gate_cases.verdict` / `gate_runs.status` String(12);
"INCOMPLETE" is 10 characters and needs room. `gate_runs.incomplete` counts the cases whose
required checks could not run (judge unavailable, execution error) — a distinct, blocking
count that must never be folded into passed/failed/skipped.

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("case_replays", "verdict", type_=sa.String(16), existing_type=sa.String(8))
    op.alter_column("gate_cases", "verdict", type_=sa.String(16), existing_type=sa.String(12))
    op.alter_column("gate_runs", "status", type_=sa.String(16), existing_type=sa.String(12))
    op.add_column(
        "gate_runs", sa.Column("incomplete", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    op.drop_column("gate_runs", "incomplete")
    op.alter_column("gate_runs", "status", type_=sa.String(12), existing_type=sa.String(16))
    op.alter_column("gate_cases", "verdict", type_=sa.String(12), existing_type=sa.String(16))
    op.alter_column("case_replays", "verdict", type_=sa.String(8), existing_type=sa.String(16))
