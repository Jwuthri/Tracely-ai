"""revocable public share links

A share token is a stateless signed capability, so until now a link could not be killed — an owner
who pasted a gate verdict into the wrong PR had to wait out its 30-day TTL. This table records the
moment sharing was stopped for one subject; `GET /api/share/{token}` refuses any token whose `iat`
predates it, which covers links the owner no longer holds a copy of.

Keyed by subject, not by token, for exactly that reason. Re-sharing mints a token with a later
`iat` and works again, so revoke is "stop sharing", not "burn this id forever".

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "share_revocations",
        sa.Column("project_id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(24), primary_key=True),
        sa.Column("subject_id", sa.String(64), primary_key=True),
        sa.Column(
            "revoked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("share_revocations")
