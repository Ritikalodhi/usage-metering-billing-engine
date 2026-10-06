"""tests/test_webhooks.py — Phase 5B: Stripe Webhooks.

All Stripe API calls are mocked; the real Stripe API is never called.
Tests use the real database through TestClient / SessionLocal.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.db.models import Plan, PlanCode, SubStatus, Subscription, Tenant, WebhookEvent
from app.db.session import SessionLocal
from app.main import app

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

FAKE_WEBHOOK_SECRET = "whsec_testsecret"


def _make_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _get_or_create_plan(session, code: PlanCode) -> Plan:
    plan = session.scalar(sa.select(Plan).where(Plan.code == code))
    if plan is None:
        plan = Plan(
            code=code,
            display_name="Free" if code == PlanCode.free else "Pro",
            api_call_limit=1000 if code == PlanCode.free else 50000,
            token_limit=100000 if code == PlanCode.free else 5000000,
        )
        session.add(plan)
        session.commit()
        session.refresh(plan)
    return plan


def _make_tenant(session, *, stripe_customer_id: str | None = None) -> tuple[Tenant, str]:
    free_plan = _get_or_create_plan(session, PlanCode.free)
    suffix = uuid.uuid4().hex[:8]
    raw_key = f"sk_test_{uuid.uuid4().hex}"
    api_key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    tenant = Tenant(
        name=f"tenant-wh-{suffix}",
        plan_id=free_plan.id,
        api_key_hash=api_key_hash,
        stripe_customer_id=stripe_customer_id,
    )
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    return tenant, raw_key


def _make_stripe_event(
    event_type: str,
    data_object: dict,
    event_id: str | None = None,
    created: int | None = None,
) -> dict:
    """Build a minimal Stripe event dict."""
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex[:16]}",
        "object": "event",
        "type": event_type,
        "created": created or int(time.time()),
        "livemode": False,
        "data": {"object": data_object},
    }


def _make_checkout_session_event(
    tenant_id: str,
    stripe_subscription_id: str,
    stripe_customer_id: str,
    event_id: str | None = None,
    created: int | None = None,
) -> dict:
    return _make_stripe_event(
        "checkout.session.completed",
        {
            "id": f"cs_{uuid.uuid4().hex[:16]}",
            "object": "checkout.session",
            "client_reference_id": tenant_id,
            "subscription": stripe_subscription_id,
            "customer": stripe_customer_id,
        },
        event_id=event_id,
        created=created,
    )


def _make_subscription_object(
    stripe_sub_id: str,
    customer_id: str,
    status: str = "active",
    period_start: int | None = None,
    period_end: int | None = None,
) -> dict:
    now = int(time.time())
    return {
        "id": stripe_sub_id,
        "object": "subscription",
        "customer": customer_id,
        "status": status,
        "current_period_start": period_start or now,
        "current_period_end": period_end or (now + 30 * 86400),
    }


def _post_webhook(client, event_dict: dict, sig_header: str = "valid_sig") -> object:
    body = json.dumps(event_dict).encode()
    return client.post(
        "/webhooks/stripe",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": sig_header,
        },
    )


def _mock_construct_event_valid(event_dict: dict):
    """Return a context manager that makes construct_event succeed with event_dict."""
    mock_event = MagicMock()
    mock_event.__getitem__ = lambda self, key: event_dict[key]
    mock_event.get = lambda key, default=None: event_dict.get(key, default)
    mock_event["id"] = event_dict["id"]
    mock_event["type"] = event_dict["type"]
    mock_event["created"] = event_dict["created"]
    mock_event["data"] = event_dict["data"]

    # Make the MagicMock subscriptable with real data
    class _Event(dict):
        pass

    real_event = _Event(event_dict)

    return patch(
        "app.api.routes.webhooks.stripe.Webhook.construct_event",
        return_value=real_event,
    )


def _cleanup_webhook_event(db, event_id: str) -> None:
    db.execute(
        sa.text("DELETE FROM webhook_events WHERE stripe_event_id = :eid"),
        {"eid": event_id},
    )
    db.commit()


def _cleanup_subscription(db, stripe_sub_id: str) -> None:
    db.execute(
        sa.text("DELETE FROM subscriptions WHERE stripe_subscription_id = :sid"),
        {"sid": stripe_sub_id},
    )
    db.commit()


# ---------------------------------------------------------------------------
# Test: Invalid signature → 400, no DB writes
# ---------------------------------------------------------------------------


def test_invalid_signature_returns_400(client, db):
    """stripe.Webhook.construct_event raises → 400, no webhook_event or subscription created."""
    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    event_dict = _make_stripe_event(
        "checkout.session.completed",
        {"id": "cs_bad", "client_reference_id": "bad"},
        event_id=event_id,
    )

    with patch(
        "app.api.routes.webhooks.stripe.Webhook.construct_event",
        side_effect=ValueError("bad sig"),
    ):
        resp = _post_webhook(client, event_dict, sig_header="bad_sig")

    assert resp.status_code == 400

    # No webhook_event row inserted
    row = db.scalar(
        sa.select(WebhookEvent).where(WebhookEvent.stripe_event_id == event_id)
    )
    assert row is None, "Webhook event must NOT be inserted on invalid signature"


# ---------------------------------------------------------------------------
# Test: checkout.session.completed
# ---------------------------------------------------------------------------


def test_checkout_session_completed_creates_subscription(client, db):
    """Valid checkout.session.completed event creates Pro subscription and upgrades tenant."""
    tenant, _ = _make_tenant(db)
    stripe_sub_id = f"sub_{uuid.uuid4().hex[:16]}"
    stripe_cus_id = f"cus_{uuid.uuid4().hex[:8]}"
    event_id = f"evt_{uuid.uuid4().hex[:16]}"

    event_dict = _make_checkout_session_event(
        tenant_id=str(tenant.id),
        stripe_subscription_id=stripe_sub_id,
        stripe_customer_id=stripe_cus_id,
        event_id=event_id,
    )

    fake_stripe_sub = _make_subscription_object(stripe_sub_id, stripe_cus_id)

    with _mock_construct_event_valid(event_dict), patch(
        "app.api.routes.webhooks.stripe.Subscription.retrieve",
        return_value=fake_stripe_sub,
    ):
        resp = _post_webhook(client, event_dict)

    assert resp.status_code == 200, resp.text

    # Webhook event recorded and processed
    db.expire_all()
    wh_row = db.scalar(
        sa.select(WebhookEvent).where(WebhookEvent.stripe_event_id == event_id)
    )
    assert wh_row is not None, "WebhookEvent row must exist"
    assert wh_row.processed_at is not None, "processed_at must be set"

    # Subscription created with Pro plan
    sub = db.scalar(
        sa.select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub_id)
    )
    assert sub is not None, "Subscription must be created"
    assert sub.status == SubStatus.active
    assert sub.tenant_id == tenant.id

    # Tenant upgraded to Pro
    db.expire_all()
    updated_tenant = db.scalar(sa.select(Tenant).where(Tenant.id == tenant.id))
    pro_plan = _get_or_create_plan(db, PlanCode.pro)
    assert updated_tenant.plan_id == pro_plan.id, "Tenant must be on Pro plan"
    assert updated_tenant.stripe_customer_id == stripe_cus_id

    # Cleanup
    _cleanup_subscription(db, stripe_sub_id)
    _cleanup_webhook_event(db, event_id)


# ---------------------------------------------------------------------------
# Test: Replay (duplicate event)
# ---------------------------------------------------------------------------


def test_replay_returns_200_without_duplicate_processing(client, db):
    """Sending the same event twice: second call returns 200 and creates no extra rows."""
    tenant, _ = _make_tenant(db)
    stripe_sub_id = f"sub_{uuid.uuid4().hex[:16]}"
    stripe_cus_id = f"cus_{uuid.uuid4().hex[:8]}"
    event_id = f"evt_{uuid.uuid4().hex[:16]}"

    event_dict = _make_checkout_session_event(
        tenant_id=str(tenant.id),
        stripe_subscription_id=stripe_sub_id,
        stripe_customer_id=stripe_cus_id,
        event_id=event_id,
    )
    fake_stripe_sub = _make_subscription_object(stripe_sub_id, stripe_cus_id)

    with _mock_construct_event_valid(event_dict), patch(
        "app.api.routes.webhooks.stripe.Subscription.retrieve",
        return_value=fake_stripe_sub,
    ):
        resp1 = _post_webhook(client, event_dict)
        assert resp1.status_code == 200

    # Second send — same event_id
    with _mock_construct_event_valid(event_dict), patch(
        "app.api.routes.webhooks.stripe.Subscription.retrieve",
        return_value=fake_stripe_sub,
    ):
        resp2 = _post_webhook(client, event_dict)

    assert resp2.status_code == 200
    assert resp2.json().get("status") == "replay"

    # Exactly one webhook_events row
    db.expire_all()
    rows = db.scalars(
        sa.select(WebhookEvent).where(WebhookEvent.stripe_event_id == event_id)
    ).all()
    assert len(rows) == 1, "Exactly one WebhookEvent row expected"

    # Exactly one subscription row
    subs = db.scalars(
        sa.select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub_id)
    ).all()
    assert len(subs) == 1, "Exactly one Subscription row expected"

    # Cleanup
    _cleanup_subscription(db, stripe_sub_id)
    _cleanup_webhook_event(db, event_id)


# ---------------------------------------------------------------------------
# Test: customer.subscription.updated (newer event)
# ---------------------------------------------------------------------------


def test_subscription_updated_newer_event(client, db):
    """A newer customer.subscription.updated event updates the subscription."""
    tenant, _ = _make_tenant(db)
    stripe_sub_id = f"sub_{uuid.uuid4().hex[:16]}"
    stripe_cus_id = f"cus_{uuid.uuid4().hex[:8]}"
    tenant.stripe_customer_id = stripe_cus_id
    db.commit()

    # Insert a subscription with an older last_event_at
    pro_plan = _get_or_create_plan(db, PlanCode.pro)
    old_ts = int(time.time()) - 3600  # 1 hour ago
    sub = Subscription(
        tenant_id=tenant.id,
        plan_id=pro_plan.id,
        stripe_subscription_id=stripe_sub_id,
        status=SubStatus.active,
        last_event_at=datetime.fromtimestamp(old_ts, tz=timezone.utc),
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)

    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    new_ts = int(time.time())  # now — newer than old_ts

    sub_obj = _make_subscription_object(
        stripe_sub_id, stripe_cus_id, status="past_due"
    )
    event_dict = _make_stripe_event(
        "customer.subscription.updated", sub_obj, event_id=event_id, created=new_ts
    )

    with _mock_construct_event_valid(event_dict):
        resp = _post_webhook(client, event_dict)

    assert resp.status_code == 200

    db.expire_all()
    updated_sub = db.scalar(
        sa.select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub_id)
    )
    assert updated_sub.status == SubStatus.past_due, "Subscription status should be updated"

    wh_row = db.scalar(
        sa.select(WebhookEvent).where(WebhookEvent.stripe_event_id == event_id)
    )
    assert wh_row is not None
    assert wh_row.processed_at is not None

    _cleanup_subscription(db, stripe_sub_id)
    _cleanup_webhook_event(db, event_id)


# ---------------------------------------------------------------------------
# Test: Out-of-order update
# ---------------------------------------------------------------------------


def test_out_of_order_subscription_updated_ignored(client, db):
    """An older event must NOT overwrite a newer subscription state."""
    tenant, _ = _make_tenant(db)
    stripe_sub_id = f"sub_{uuid.uuid4().hex[:16]}"
    stripe_cus_id = f"cus_{uuid.uuid4().hex[:8]}"
    tenant.stripe_customer_id = stripe_cus_id
    db.commit()

    pro_plan = _get_or_create_plan(db, PlanCode.pro)
    new_ts = int(time.time())  # "current" last_event_at
    sub = Subscription(
        tenant_id=tenant.id,
        plan_id=pro_plan.id,
        stripe_subscription_id=stripe_sub_id,
        status=SubStatus.active,
        last_event_at=datetime.fromtimestamp(new_ts, tz=timezone.utc),
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)

    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    old_ts = new_ts - 3600  # 1 hour older than stored last_event_at

    sub_obj = _make_subscription_object(
        stripe_sub_id, stripe_cus_id, status="canceled"
    )
    event_dict = _make_stripe_event(
        "customer.subscription.updated", sub_obj, event_id=event_id, created=old_ts
    )

    with _mock_construct_event_valid(event_dict):
        resp = _post_webhook(client, event_dict)

    assert resp.status_code == 200

    # Subscription state must be unchanged
    db.expire_all()
    unchanged_sub = db.scalar(
        sa.select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub_id)
    )
    assert unchanged_sub.status == SubStatus.active, (
        "Out-of-order event must NOT overwrite subscription state"
    )

    # Webhook event must still be recorded (processed)
    wh_row = db.scalar(
        sa.select(WebhookEvent).where(WebhookEvent.stripe_event_id == event_id)
    )
    assert wh_row is not None, "Webhook event must be recorded even for out-of-order events"
    assert wh_row.processed_at is not None

    _cleanup_subscription(db, stripe_sub_id)
    _cleanup_webhook_event(db, event_id)


# ---------------------------------------------------------------------------
# Test: customer.subscription.deleted
# ---------------------------------------------------------------------------


def test_subscription_deleted_cancels_and_reverts_to_free(client, db):
    """customer.subscription.deleted cancels subscription and reverts tenant to Free plan."""
    tenant, _ = _make_tenant(db)
    stripe_sub_id = f"sub_{uuid.uuid4().hex[:16]}"
    stripe_cus_id = f"cus_{uuid.uuid4().hex[:8]}"
    tenant.stripe_customer_id = stripe_cus_id

    pro_plan = _get_or_create_plan(db, PlanCode.pro)
    tenant.plan_id = pro_plan.id
    db.commit()

    sub = Subscription(
        tenant_id=tenant.id,
        plan_id=pro_plan.id,
        stripe_subscription_id=stripe_sub_id,
        status=SubStatus.active,
        last_event_at=datetime.fromtimestamp(int(time.time()) - 3600, tz=timezone.utc),
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)

    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    sub_obj = _make_subscription_object(stripe_sub_id, stripe_cus_id, status="canceled")
    event_dict = _make_stripe_event(
        "customer.subscription.deleted", sub_obj, event_id=event_id
    )

    with _mock_construct_event_valid(event_dict):
        resp = _post_webhook(client, event_dict)

    assert resp.status_code == 200

    db.expire_all()

    canceled_sub = db.scalar(
        sa.select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub_id)
    )
    assert canceled_sub.status == SubStatus.canceled, "Subscription must be canceled"

    reverted_tenant = db.scalar(sa.select(Tenant).where(Tenant.id == tenant.id))
    free_plan = _get_or_create_plan(db, PlanCode.free)
    assert reverted_tenant.plan_id == free_plan.id, "Tenant must revert to Free plan"

    wh_row = db.scalar(
        sa.select(WebhookEvent).where(WebhookEvent.stripe_event_id == event_id)
    )
    assert wh_row is not None
    assert wh_row.processed_at is not None

    _cleanup_subscription(db, stripe_sub_id)
    _cleanup_webhook_event(db, event_id)


# ---------------------------------------------------------------------------
# Test: Unknown event type
# ---------------------------------------------------------------------------


def test_unknown_event_type_returns_200_and_records(client, db):
    """Unknown event types return 200 and are recorded; subscription state unchanged."""
    event_id = f"evt_{uuid.uuid4().hex[:16]}"
    event_dict = _make_stripe_event(
        "payment_intent.created",
        {"id": f"pi_{uuid.uuid4().hex[:16]}", "amount": 1000},
        event_id=event_id,
    )

    with _mock_construct_event_valid(event_dict):
        resp = _post_webhook(client, event_dict)

    assert resp.status_code == 200

    db.expire_all()
    wh_row = db.scalar(
        sa.select(WebhookEvent).where(WebhookEvent.stripe_event_id == event_id)
    )
    assert wh_row is not None, "Webhook event must be recorded for unknown types"
    assert wh_row.processed_at is not None
    assert wh_row.type == "payment_intent.created"

    _cleanup_webhook_event(db, event_id)


# ---------------------------------------------------------------------------
# Test: Raw body is read before construct_event (structural verification)
# ---------------------------------------------------------------------------


def test_raw_body_read_before_construct_event(client):
    """Verify implementation reads raw_body before calling stripe.Webhook.construct_event.

    We intercept construct_event and capture the first positional argument.
    It must be bytes (the raw body), not a parsed dict or Pydantic model.
    """
    body_bytes = b'{"id":"evt_rawbodytest","type":"payment_intent.created","data":{}}'
    captured = {}

    def fake_construct_event(payload, sig_header, secret):
        captured["payload"] = payload
        captured["payload_type"] = type(payload)
        raise ValueError("deliberate failure to produce 400")

    with patch(
        "app.api.routes.webhooks.stripe.Webhook.construct_event",
        side_effect=fake_construct_event,
    ):
        resp = client.post(
            "/webhooks/stripe",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "t=1,v1=fakesig",
            },
        )

    assert resp.status_code == 400
    assert "payload" in captured, "construct_event must have been called"
    assert isinstance(captured["payload"], bytes), (
        f"construct_event must receive raw bytes, got {captured['payload_type']}"
    )
    assert captured["payload"] == body_bytes, (
        "construct_event must receive the original raw body unchanged"
    )
