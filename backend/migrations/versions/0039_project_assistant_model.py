"""per-workspace assistant model (`projects.assistant_model`)

The in-app assistant answered on one server-wide model (`Settings.assistant_model`). A workspace
now picks its own OpenRouter model id; NULL = the server default.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("assistant_model", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "assistant_model")
