"""activation milestones (W6): first real outcomes per workspace, sample-vs-real, durable

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_milestones",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), primary_key=True),
        sa.Column("name", sa.String(48), primary_key=True),
        sa.Column("first_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("sample", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("integration", sa.String(64), nullable=False, server_default=""),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("project_milestones")
