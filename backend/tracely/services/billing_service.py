"""Stripe subscription billing — checkout, portal, and the webhook state machine.

The subscription belongs to the ORGANIZATION, not to a workspace: a team pays once and every
workspace in the account is covered.

The `stripe` import lives here and nowhere else. Everything is driven by webhooks: checkout and
the portal only mint redirect URLs; the plan on the Organization row changes exclusively when
Stripe tells us so (idempotent state-sets, safe under replay and out-of-order delivery).

Two ordering hazards, both handled explicitly, because Stripe guarantees neither order nor
single delivery:

1. `customer.subscription.*` events carry no `client_reference_id` and can arrive BEFORE
   `checkout.session.completed` stores the customer id. Checkout therefore stamps
   `subscription_data.metadata.organization_id` onto the subscription itself, and the webhook
   falls back to that metadata when the customer id lookup misses — backfilling the ids so the
   next event takes the fast path.
2. An OLDER event can arrive after a newer one — out of order on first delivery, or redelivered
   from Stripe's retry schedule days later. Idempotent state-sets survive a replay of the SAME
   event but not of a stale one: `subscription.updated(active)` landing after
   `subscription.deleted` would put a cancelled account back on Pro, for free, with nothing in
   the logs to say why. `organizations.subscription_event_at` records the newest event applied,
   and anything strictly older is dropped.
"""

from __future__ import annotations

import structlog
from sqlalchemy.orm import Session

from tracely.config import settings
from tracely.domain.billing import (
    PLAN_UNLIMITED,
    plan_for_subscription_status,
)
from tracely.infrastructure.db import repositories
from tracely.infrastructure.db.models import Organization

log = structlog.get_logger()


def stripe_configured() -> bool:
    return bool(settings.stripe_secret_key and settings.stripe_price_pro)


def _stripe():
    import stripe

    stripe.api_key = settings.stripe_secret_key
    return stripe


def _billing_url() -> str:
    return f"{settings.app_base_url.rstrip('/')}/settings/billing"


def create_checkout_session(session: Session, organization_id: str) -> str:
    """A Stripe Checkout URL upgrading this organization to Pro. Raises ValueError for the
    conditions the router maps to 4xx (already subscribed / unknown org)."""
    org = repositories.organization_get(session, organization_id)
    if org is None:
        raise ValueError("organization not found")
    if (org.subscription_status or "") in ("active", "trialing", "past_due"):
        raise ValueError("already subscribed — manage the existing subscription instead")

    stripe = _stripe()
    customer_id = org.stripe_customer_id
    if not customer_id:
        customer = stripe.Customer.create(
            name=org.name or org.slug,
            metadata={"organization_id": org.id, "organization_slug": org.slug},
        )
        customer_id = customer["id"]
        org.stripe_customer_id = customer_id
        session.commit()

    checkout = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": settings.stripe_price_pro, "quantity": 1}],
        client_reference_id=org.id,
        # Load-bearing: subscription events don't carry client_reference_id, and can arrive
        # before checkout.session.completed — this metadata is the webhook's fallback lookup.
        subscription_data={"metadata": {"organization_id": org.id}},
        success_url=f"{_billing_url()}?upgraded=1",
        cancel_url=_billing_url(),
        allow_promotion_codes=True,
    )
    return checkout["url"]


def create_portal_session(session: Session, organization_id: str) -> str:
    """A Stripe Billing Portal URL (change card, cancel, invoices). ValueError when the
    organization has never been through checkout."""
    org = repositories.organization_get(session, organization_id)
    if org is None or not org.stripe_customer_id:
        raise ValueError("no billing account yet — upgrade first")
    portal = _stripe().billing_portal.Session.create(
        customer=org.stripe_customer_id, return_url=_billing_url()
    )
    return portal["url"]


def verify_webhook(payload: bytes, signature: str):
    """The parsed, signature-verified event — raises on any tampering/malformed input.
    Verification needs the raw request bytes, never a re-serialized body."""
    import stripe

    return stripe.Webhook.construct_event(
        payload, signature, settings.stripe_webhook_secret
    )


def handle_webhook_event(session: Session, event) -> dict:
    """Apply one verified Stripe event. Idempotent state-sets throughout.

    Return contract (the router's response depends on it): normal returns — including unknown
    event types, unknown organizations and stale events — mean 200 (permanent no-ops must not be
    redelivered); raising means 5xx, so Stripe's retry schedule redelivers after a transient
    failure instead of a paid upgrade being silently swallowed.
    """
    etype = event["type"]
    obj = event["data"]["object"]
    # `created` is on every Stripe event. A forged/hand-built event without one reads as 0, which
    # is older than anything already applied — so it is dropped rather than trusted.
    created = int(event.get("created") or 0)

    if etype == "checkout.session.completed":
        org = repositories.organization_get(session, obj.get("client_reference_id") or "")
        if org is None:
            log.warning("stripe_checkout_unknown_org", ref=obj.get("client_reference_id"))
            return {"handled": False}
        if _is_stale(org, created, etype):
            return {"handled": False, "stale": True}
        _apply_subscription(
            session, org,
            customer_id=obj.get("customer"),
            subscription_id=obj.get("subscription"),
            # Checkout completing means payment succeeded; the subscription.updated that follows
            # carries the authoritative status and converges to the same state.
            status="active",
            event_at=created,
        )
        return {"handled": True, "plan": org.plan}

    if etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        status = "canceled" if etype.endswith("deleted") else (obj.get("status") or "")
        org = repositories.organization_by_stripe_customer(session, obj.get("customer") or "")
        if org is None:
            # Out-of-order delivery: the checkout event hasn't stored the customer id yet.
            org = repositories.organization_get(
                session, (obj.get("metadata") or {}).get("organization_id") or ""
            )
        if org is None:
            log.warning("stripe_subscription_unknown_org", customer=obj.get("customer"))
            return {"handled": False}
        if _is_stale(org, created, etype):
            return {"handled": False, "stale": True}
        _apply_subscription(
            session, org,
            customer_id=obj.get("customer"),
            subscription_id=obj.get("id"),
            status=status,
            event_at=created,
        )
        return {"handled": True, "plan": org.plan}

    return {"handled": False, "ignored": etype}


def _is_stale(org: Organization, created: int, etype: str) -> bool:
    """Whether this event predates the one already applied to the org.

    Strictly older, so the two events of a single upgrade (checkout + subscription.updated) can
    share a second and both apply — they converge on the same state anyway. Equal-or-newer wins,
    which also keeps a plain redelivery of the newest event working exactly as before.
    """
    applied = org.subscription_event_at
    if applied is None or created >= applied:
        return False
    log.info(
        "stripe_event_stale_ignored",
        organization_id=org.id, event_type=etype, created=created, applied=applied,
    )
    return True


def _apply_subscription(
    session: Session, org: Organization, *, customer_id: str | None,
    subscription_id: str | None, status: str, event_at: int = 0,
) -> None:
    if customer_id:
        org.stripe_customer_id = customer_id
    if subscription_id:
        org.stripe_subscription_id = subscription_id
    org.subscription_status = status
    org.subscription_event_at = max(event_at, org.subscription_event_at or 0)
    # Never touch an operator account: `unlimited` is set via SQL and owns its plan outright —
    # a stray subscription event against it records the status but must not downgrade the plan.
    if org.plan != PLAN_UNLIMITED:
        org.plan = plan_for_subscription_status(status)
    session.commit()
    log.info(
        "stripe_subscription_applied",
        organization_id=org.id, plan=org.plan, status=status,
    )
