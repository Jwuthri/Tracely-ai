"""candidate provenance + honest verification state (W4)

`gate_runs.run_id` / `execution_mode`: the manifest of what produced a gate's candidates — the CI
execution identity every emitted span carries as `tracely.replay.run_id`, and the mode that ran.
`evaluation_cases.verified_*`: the first time a SPECIFIC candidate execution passed the contract,
at which case version. `fail_to_pass_validated` keeps meaning "the source fails" and is never
migrated into the new columns — historical source-failure evidence is not a verified fix.

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("gate_runs", sa.Column("run_id", sa.String(64), nullable=False, server_default=""))
    op.add_column(
        "gate_runs", sa.Column("execution_mode", sa.String(16), nullable=False, server_default="")
    )
    op.add_column(
        "evaluation_cases",
        sa.Column("verified_candidate_trace_id", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column("evaluation_cases", sa.Column("verified_case_version", sa.Integer(), nullable=True))
    op.add_column(
        "evaluation_cases", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "evaluation_cases", sa.Column("verified_by", sa.String(64), nullable=False, server_default="")
    )


def downgrade() -> None:
    for col in ("verified_by", "verified_at", "verified_case_version", "verified_candidate_trace_id"):
        op.drop_column("evaluation_cases", col)
    op.drop_column("gate_runs", "execution_mode")
    op.drop_column("gate_runs", "run_id")
