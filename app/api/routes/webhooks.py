"""POST /webhooks/stripe — Phase 5B: Stripe Webhooks.

Design decisions:
- Raw body is read first, before any parsing or verification.
- Signature verification via stripe.Webhook.construct_event; any failure → 400, no DB writes.
- Deduplication uses INSERT … ON CONFLICT (stripe_event_id) DO NOTHING RETURNING to avoid
  a read-then-insert race.
- All DB mutations (webhook_events insert + subscription update) are inside a single
  transaction so failures roll back atomically.
- Out-of-order protection: if the stored last_event_at is newer than the incoming Stripe
  event's created timestamp, skip the subscription update but still commit the webhook row.
"""
from __future__ import annotations

import json as _json
import logging
from datetime import datetime, timezone

import sqlalchemy as sa
import stripe
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config.settings import settings
from app.db.models import Plan, PlanCode, SubStatus, Subscription, Tenant, WebhookEvent
from app.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

# Initialise Stripe key once at import time (same pattern as stripe_sync.py).
stripe.api_key = settings.STRIPE_API_KEY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _from_ts(ts: int | None) -> datetime | None:
    """Convert a Unix timestamp to a timezone-aware datetime, or None."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _get_plan(db: Session, code: PlanCode) -> Plan | None:
    return db.scalar(sa.select(Plan).where(Plan.code == code))


def _get_tenant_by_id(db: Session, tenant_id: str) -> Tenant | None:
    import uuid as _uuid
    try:
        uid = _uuid.UUID(tenant_id)
    except (ValueError, AttributeError):
        return None
    return db.scalar(sa.select(Tenant).where(Tenant.id == uid))


def _get_tenant_by_customer(db: Session, customer_id: str) -> Tenant | None:
    return db.scalar(sa.select(Tenant).where(Tenant.stripe_customer_id == customer_id))


def _get_subscription_by_stripe_id(db: Session, stripe_sub_id: str) -> Subscription | None:
    return db.scalar(
        sa.select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub_id)
    )


def _insert_webhook_event_returning(db: Session, event: stripe.Event) -> bool:
    """Insert the webhook event row.

    Returns True if this is a new (non-duplicate) event, False if it was
    already processed (ON CONFLICT DO NOTHING → no row returned).
    """
    result = db.execute(
        sa.text(
            """
            INSERT INTO webhook_events (stripe_event_id, type, payload, received_at)
            VALUES (:eid, :etype, CAST(:payload AS jsonb), now())
            ON CONFLICT (stripe_event_id) DO NOTHING
            RETURNING stripe_event_id
            """
        ),
        {
            "eid": event["id"],
            "etype": event["type"],
            "payload": _json.dumps(dict(event)),
        },
    )
    row = result.fetchone()
    return row is not None


def _mark_processed(db: Session, stripe_event_id: str) -> None:
    db.execute(
        sa.text(
            "UPDATE webhook_events SET processed_at = now() WHERE stripe_event_id = :eid"
        ),
        {"eid": stripe_event_id},
    )


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


def _handle_checkout_session_completed(
    db: Session, event: stripe.Event
) -> JSONResponse | None:
    """Create or update subscription from a completed Checkout Session.

    Returns a JSONResponse only on error; None on success.
    """
    session_obj = event["data"]["object"]
    tenant_id_str: str | None = session_obj.get("client_reference_id")
    stripe_subscription_id: str | None = session_obj.get("subscription")
    stripe_customer_id: str | None = session_obj.get("customer")

    # Locate tenant
    tenant = _get_tenant_by_id(db, tenant_id_str or "")
    if tenant is None:
        logger.error(
            "checkout.session.completed: tenant not found for client_reference_id=%r",
            tenant_id_str,
        )
        return JSONResponse(status_code=500, content={"detail": "Tenant not found"})

    # Update customer ID if not already stored
    if stripe_customer_id and not tenant.stripe_customer_id:
        tenant.stripe_customer_id = stripe_customer_id

    # Retrieve full subscription from Stripe
    stripe_sub = stripe.Subscription.retrieve(stripe_subscription_id)
    event_ts = datetime.fromtimestamp(event["created"], tz=timezone.utc)

    # Locate Pro plan
    pro_plan = _get_plan(db, PlanCode.pro)
    if pro_plan is None:
        logger.error("checkout.session.completed: Pro plan not found in database")
        return JSONResponse(status_code=500, content={"detail": "Pro plan not found"})

    # Map Stripe status
    raw_status = stripe_sub.get("status", "active")
    try:
        sub_status = SubStatus(raw_status)
    except ValueError:
        sub_status = SubStatus.active

    period_start = _from_ts(stripe_sub.get("current_period_start"))
    period_end = _from_ts(stripe_sub.get("current_period_end"))

    # Upsert subscription
    existing_sub = _get_subscription_by_stripe_id(db, stripe_subscription_id)
    if existing_sub is not None:
        existing_sub.plan_id = pro_plan.id
        existing_sub.status = sub_status
        existing_sub.current_period_start = period_start
        existing_sub.current_period_end = period_end
        existing_sub.last_event_at = event_ts
    else:
        new_sub = Subscription(
            tenant_id=tenant.id,
            plan_id=pro_plan.id,
            stripe_subscription_id=stripe_subscription_id,
            status=sub_status,
            current_period_start=period_start,
            current_period_end=period_end,
            last_event_at=event_ts,
        )
        db.add(new_sub)

    # Upgrade tenant plan
    tenant.plan_id = pro_plan.id
    return None


def _handle_subscription_updated(db: Session, event: stripe.Event) -> None:
    """Update an existing subscription from customer.subscription.updated."""
    stripe_sub_obj = event["data"]["object"]
    stripe_subscription_id: str = stripe_sub_obj["id"]
    stripe_customer_id: str | None = stripe_sub_obj.get("customer")
    event_ts = datetime.fromtimestamp(event["created"], tz=timezone.utc)

    existing_sub = _get_subscription_by_stripe_id(db, stripe_subscription_id)
    if existing_sub is None:
        logger.warning(
            "customer.subscription.updated: subscription not found id=%s",
            stripe_subscription_id,
        )
        return

    # Out-of-order guard
    if existing_sub.last_event_at and event_ts <= existing_sub.last_event_at:
        logger.info(
            "customer.subscription.updated: skipping out-of-order event "
            "event_ts=%s last_event_at=%s",
            event_ts,
            existing_sub.last_event_at,
        )
        return  # Do not modify subscription; webhook row still committed above

    raw_status = stripe_sub_obj.get("status", "active")
    try:
        sub_status = SubStatus(raw_status)
    except ValueError:
        sub_status = SubStatus.active

    period_start = _from_ts(stripe_sub_obj.get("current_period_start"))
    period_end = _from_ts(stripe_sub_obj.get("current_period_end"))

    existing_sub.status = sub_status
    existing_sub.stripe_subscription_id = stripe_subscription_id
    existing_sub.current_period_start = period_start
    existing_sub.current_period_end = period_end
    existing_sub.last_event_at = event_ts

    if stripe_customer_id:
        tenant = db.scalar(sa.select(Tenant).where(Tenant.id == existing_sub.tenant_id))
        if tenant and not tenant.stripe_customer_id:
            tenant.stripe_customer_id = stripe_customer_id


def _handle_subscription_deleted(db: Session, event: stripe.Event) -> None:
    """Cancel subscription and revert tenant to Free plan on customer.subscription.deleted."""
    stripe_sub_obj = event["data"]["object"]
    stripe_subscription_id: str = stripe_sub_obj["id"]
    event_ts = datetime.fromtimestamp(event["created"], tz=timezone.utc)

    existing_sub = _get_subscription_by_stripe_id(db, stripe_subscription_id)
    if existing_sub is None:
        logger.warning(
            "customer.subscription.deleted: subscription not found id=%s",
            stripe_subscription_id,
        )
        return

    existing_sub.status = SubStatus.canceled
    existing_sub.last_event_at = event_ts

    # Revert tenant to Free plan
    free_plan = _get_plan(db, PlanCode.free)
    if free_plan is not None:
        tenant = db.scalar(sa.select(Tenant).where(Tenant.id == existing_sub.tenant_id))
        if tenant is not None:
            tenant.plan_id = free_plan.id


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Receive and process Stripe webhook events.

    Security: raw body is read first; signature is verified via
    stripe.Webhook.construct_event before any DB interaction.
    """
    # 1. Read the raw body — MUST happen before any other processing.
    raw_body = await request.body()

    # 2. Read the Stripe-Signature header.
    sig_header = request.headers.get("Stripe-Signature")

    # 3. Verify signature and construct the event.
    try:
        event = stripe.Webhook.construct_event(
            raw_body,
            sig_header,
            settings.STRIPE_WEBHOOK_SECRET,
        )
    except Exception:
        # Do NOT expose exception details; do NOT touch the database.
        logger.warning("Stripe webhook signature verification failed")
        return JSONResponse(status_code=400, content={"detail": "Invalid signature"})

    # 4. Everything from here is inside a single transaction.
    try:
        # 4a. Deduplicate: INSERT … ON CONFLICT DO NOTHING RETURNING
        is_new = _insert_webhook_event_returning(db, event)
        if not is_new:
            # Replay — already processed; nothing new to commit.
            db.rollback()
            return JSONResponse(status_code=200, content={"status": "replay"})

        # 4b. Dispatch to event-specific handler.
        event_type: str = event["type"]
        error_response: JSONResponse | None = None

        if event_type == "checkout.session.completed":
            error_response = _handle_checkout_session_completed(db, event)

        elif event_type == "customer.subscription.updated":
            _handle_subscription_updated(db, event)

        elif event_type == "customer.subscription.deleted":
            _handle_subscription_deleted(db, event)

        # else: unknown event type — record it but don't modify subscription state.

        if error_response is not None:
            db.rollback()
            return error_response

        # 4c. Mark the webhook event as processed, then commit.
        _mark_processed(db, event["id"])
        db.commit()
        return JSONResponse(status_code=200, content={"status": "ok"})

    except Exception:
        logger.exception("Error processing Stripe webhook event id=%s", event.get("id"))
        db.rollback()
        return JSONResponse(
            status_code=500, content={"detail": "Internal error processing webhook"}
        )
