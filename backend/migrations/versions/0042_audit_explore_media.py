"""audits: the deepen stage of an explore run, and media attached to seeds.

- `audit_runs.stage`        — 0 while an explore run's first pass is going, 1 once its deepen
                              pass has been planned. Moved by compare-and-swap, so two ticks that
                              end the first pass together plan one second pass.
- `audit_seeds.attachments` — `[{asset, mime, name}]`: images / audio the attacker may send with
                              this seed. `asset` is a sha256 into the blob store, never bytes.

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_runs", sa.Column("stage", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("audit_seeds", sa.Column("attachments", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("audit_seeds", "attachments")
    op.drop_column("audit_runs", "stage")
