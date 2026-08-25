"""workspace-level UI preferences (starting with the hidden step-type filter)

`projects.ui_prefs` is a small JSON bag of workspace UI defaults — today only
`hiddenTypes` (span types the trace table / timeline / replay hide by default).
A browser's own localStorage filter overrides it when explicitly set; absent any
local choice, everyone in the workspace starts from this default. NULL = no
workspace defaults, identical to today's behavior.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("ui_prefs", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "ui_prefs")
