"""stripe webhooks: remember the newest event applied, so a stale one can't re-apply

Stripe does not guarantee event ORDER and retries undelivered events for up to three days. Every
webhook write here is an idempotent state-set, which is safe under replay of the SAME event but
not under replay of an OLDER one: a `customer.subscription.updated(active)` redelivered after a
`customer.subscription.deleted` would put a cancelled account back on Pro, for free, silently.

`organizations.subscription_event_at` holds the `created` of the newest event applied to that
org; `handle_webhook_event` ignores anything strictly older. NULL means no event has been applied
yet, so nothing about existing rows changes.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("subscription_event_at", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "subscription_event_at")
