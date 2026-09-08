"""durable case artifacts (W3): where a case's executable snapshot lives, and its content digest

`evaluation_cases.artifact_s3_key` points at `{prefix}cases/{project}/{case}/v{version}.json`
(input, fixtures, expectations, judge identity, provenance); `artifact_digest` is the sha256 of
that blob, pinned onto every gate result graded against it. Empty on legacy cases until the
backfill (nightly, before the retention sweep) snapshots them from their still-present sources.

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "evaluation_cases",
        sa.Column("artifact_s3_key", sa.String(512), nullable=False, server_default=""),
    )
    op.add_column(
        "evaluation_cases",
        sa.Column("artifact_digest", sa.String(64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("evaluation_cases", "artifact_digest")
    op.drop_column("evaluation_cases", "artifact_s3_key")
