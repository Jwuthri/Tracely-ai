"""Billing plans — pure logic, no I/O.

The one place the plan vocabulary and the quota arithmetic live, so the worker's counter, the
OTLP edge gate, the usage endpoint and the Stripe webhook can never disagree about what a plan
means or which month it is. Limits are passed in (from `Settings`) rather than imported, keeping
this module import-free of config.
"""

from __future__ import annotations

from datetime import datetime, timezone

PLAN_FREE = "free"
PLAN_PRO = "pro"
# Operator orgs: no cap, set via SQL, and `plan_for_subscription_status` never returns it —
# a stray Stripe event must not be able to downgrade (or grant) it.
PLAN_UNLIMITED = "unlimited"

# Organization kinds. A personal account is one human: 1 workspace, 1 seat, un-joinable — which
# is what stops "invite yourself into a second free account" from being a quota loophole.
KIND_PERSONAL = "personal"
KIND_COMPANY = "company"

# Subscription statuses that keep a workspace on the paid plan. `past_due` stays paid on
# purpose: dunning (a failed card retry) must not flip a customer to free mid-cycle — Stripe
# moves the subscription to `canceled`/`unpaid` when it gives up, and THAT downgrades.
PAID_STATUSES = frozenset({"active", "trialing", "past_due"})


def current_period(now: datetime | None = None) -> str:
    """The UTC calendar month quotas count in, as `YYYY-MM`."""
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m")


def trace_limit_for(plan: str, free_limit: int, pro_limit: int) -> int | None:
    """Monthly ingested-trace cap for a plan; None = uncapped. Unknown plan values fall back to
    the free limit — failing open to unlimited on a typo'd row would defeat the quota."""
    if plan == PLAN_UNLIMITED:
        return None
    if plan == PLAN_PRO:
        return pro_limit
    return free_limit


def retention_days_for(plan: str, free_days: int, pro_days: int) -> int | None:
    """How long a plan keeps traces; None = keep whatever the table TTL keeps.

    The ClickHouse TTL is the floor for everyone (see `ddl/0003_events_ttl`); this only ever
    shortens it, so `unlimited` returns None and Pro returns the TTL horizon (= nothing to
    sweep). Unknown plan values fall back to the free window on purpose — the same
    fail-toward-the-cap rule as `trace_limit_for`, minus the data loss risk, because a project
    only reaches this function when billing is on.
    """
    if plan == PLAN_UNLIMITED:
        return None
    if plan == PLAN_PRO:
        return pro_days
    return free_days


def workspace_limit_for(plan: str, kind: str, free_limit: int, pro_limit: int) -> int | None:
    """How many workspaces an org may hold; None = uncapped. A personal account is 1 by
    definition — upgrading it doesn't buy more, converting to a company does."""
    if plan == PLAN_UNLIMITED:
        return None
    if kind == KIND_PERSONAL:
        return 1
    return pro_limit if plan == PLAN_PRO else free_limit


def seat_limit_for(plan: str, kind: str, free_limit: int, pro_limit: int) -> int | None:
    """How many people an org may hold (members + pending invites); None = uncapped."""
    if plan == PLAN_UNLIMITED:
        return None
    if kind == KIND_PERSONAL:
        return 1
    return pro_limit if plan == PLAN_PRO else free_limit


def plan_for_subscription_status(status: str | None) -> str:
    """The plan a Stripe subscription status maps to. Only ever free|pro — see PLAN_UNLIMITED."""
    return PLAN_PRO if (status or "") in PAID_STATUSES else PLAN_FREE


def usage_fraction(used: int, limit: int | None) -> float:
    """0.0–…: how much of the cap is consumed (0.0 when uncapped). >1.0 means over."""
    if not limit or limit <= 0:
        return 0.0
    return used / limit
