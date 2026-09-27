"""per-workspace rolling-summary budget (`projects.rolling_summary_config`)

The rolling summary behind `@HISTORY` / `@ROLLING_SUMMARY` was sized by two server-wide settings
nobody could change. Workspaces now set their own: `{max_tokens, step_max_tokens}`; NULL = the
server defaults (`Settings.rolling_summary_*`).

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("rolling_summary_config", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "rolling_summary_config")
