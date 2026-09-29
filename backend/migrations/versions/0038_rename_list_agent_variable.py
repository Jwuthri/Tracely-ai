"""rename the `@LIST_AGENT` template variable to `@AGENTS`

`@LIST_AGENT` read as "every agent in the workspace" when it was always this conversation's agents.
It is now `@AGENTS` (with `.tools` / `.called`), and the old name no longer resolves — so the
evaluators that stored it in a prompt are rewritten rather than left rendering
`[No LIST_AGENT available]`.

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rename(old: str, new: str) -> None:
    op.execute(
        f"UPDATE evaluators SET config = replace(config::text, '@{old}', '@{new}')::json "
        f"WHERE config::text LIKE '%@{old}%'"
    )


def upgrade() -> None:
    _rename("LIST_AGENT", "AGENTS")


def downgrade() -> None:
    # `@AGENTS.tools` / `.called` have no old equivalent; they become a bare `@LIST_AGENT`.
    op.execute(
        "UPDATE evaluators SET config = regexp_replace(config::text, '@AGENTS(\\.(tools|called))?', "
        "'@LIST_AGENT', 'g')::json WHERE config::text LIKE '%@AGENTS%'"
    )
