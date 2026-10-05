"""Phase 4 tests: GET /v1/usage.

All tenants use the prefix ``tenant-usage-`` so that conftest cleanup picks them
up automatically at session teardown.

Test coverage:
  A. Empty usage
  B. Usage after generate
  C. Database cost cross-check
  D. Plan isolation (Free vs Pro)
  E. Previous-month exclusion (manually dated past row)
  F. Authentication (401 behaviour mirrors /v1/generate)
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.config.pricing import PRICING
from app.db.models import (
    IdempotencyKey,
    IdemState,
    Plan,
    PlanCode,
    Tenant,
    TokenCategory,
    UsageEvent,
    UsageType,
)
from app.db.session import SessionLocal
from app.main import app
from app.services.cost import price_api_call, price_tokens


# ─────────────────────────────── fixtures ────────────────────────────────────


@pytest.fixture
def client():
    return TestClient(app)


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
        if code == PlanCode.free:
            plan = Plan(
                code=PlanCode.free,
                display_name="Free",
                api_call_limit=1_000,
                token_limit=100_000,
            )
        else:
            plan = Plan(
                code=PlanCode.pro,
                display_name="Pro",
                api_call_limit=50_000,
                token_limit=5_000_000,
            )
        session.add(plan)
        session.commit()
        session.refresh(plan)
    return plan


def _make_tenant(session, plan: Plan, suffix: str | None = None) -> tuple[Tenant, str]:
    """Create a tenant and return (tenant, raw_api_key)."""
    suffix = suffix or uuid.uuid4().hex[:8]
    raw_key = f"sk_usage_{uuid.uuid4().hex}"
    api_key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    tenant = Tenant(
        name=f"tenant-usage-{suffix}",
        plan_id=plan.id,
        api_key_hash=api_key_hash,
    )
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    return tenant, raw_key


def _auth_header(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


def _generate(client: TestClient, raw_key: str, payload: dict) -> dict:
    """Helper: POST /v1/generate and assert 200."""
    resp = client.post(
        "/v1/generate",
        json=payload,
        headers={
            **_auth_header(raw_key),
            "Idempotency-Key": f"key-{uuid.uuid4().hex}",
        },
    )
    assert resp.status_code == 200, f"generate failed: {resp.text}"
    return resp.json()


# ─────────────────────────── A. Empty usage ───────────────────────────────────


def test_empty_usage_returns_zeros(client, db):
    plan = _get_or_create_plan(db, PlanCode.free)
    tenant, raw_key = _make_tenant(db, plan)

    resp = client.get("/v1/usage", headers=_auth_header(raw_key))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["api_calls"]["used"] == 0
    assert data["tokens"]["used"] == 0
    by_cat = data["tokens"]["by_category"]
    assert by_cat["input"] == 0
    assert by_cat["cached_input"] == 0
    assert by_cat["output"] == 0
    assert by_cat["reasoning"] == 0

    # Plan limits
    assert data["api_calls"]["limit"] == plan.api_call_limit
    assert data["tokens"]["limit"] == plan.token_limit
    assert data["plan"] == "free"


# ─────────────────────── B. Usage after generate ──────────────────────────────


def test_usage_after_generate_matches_exactly(client, db):
    plan = _get_or_create_plan(db, PlanCode.free)
    tenant, raw_key = _make_tenant(db, plan)

    payload = {
        "input_tokens": 100,
        "cached_input_tokens": 50,
        "output_tokens": 20,
        "reasoning_tokens": 10,
    }
    _generate(client, raw_key, payload)

    resp = client.get("/v1/usage", headers=_auth_header(raw_key))
    assert resp.status_code == 200
    data = resp.json()

    assert data["api_calls"]["used"] == 1

    by_cat = data["tokens"]["by_category"]
    assert by_cat["input"] == 100
    assert by_cat["cached_input"] == 50
    assert by_cat["output"] == 20
    assert by_cat["reasoning"] == 10

    total_tokens = 100 + 50 + 20 + 10
    assert data["tokens"]["used"] == total_tokens

    # Recompute expected cost independently
    expected_token_cost = price_tokens(
        input=100, cached_input=50, output=20, reasoning=10
    )
    expected_api_cost = price_api_call(1)
    expected_total = expected_api_cost + expected_token_cost["total_micro_cents"]
    assert data["cost_micro_cents"] == expected_total


# ──────────────────── C. Database cost cross-check ────────────────────────────


def test_cost_equals_stored_event_sum(client, db):
    """GET /v1/usage cost_micro_cents must equal the sum of stored event rows."""
    plan = _get_or_create_plan(db, PlanCode.free)
    tenant, raw_key = _make_tenant(db, plan)

    payload = {
        "input_tokens": 200,
        "cached_input_tokens": 100,
        "output_tokens": 40,
        "reasoning_tokens": 5,
    }
    _generate(client, raw_key, payload)

    # Sum stored cost_micro_cents from DB for current period
    month_start = db.execute(
        sa.select(sa.func.date_trunc("month", sa.func.now()))
    ).scalar_one()
    stored_sum = db.scalar(
        sa.select(sa.func.sum(UsageEvent.cost_micro_cents)).where(
            UsageEvent.tenant_id == tenant.id,
            UsageEvent.occurred_at >= month_start,
        )
    ) or 0

    resp = client.get("/v1/usage", headers=_auth_header(raw_key))
    assert resp.status_code == 200
    api_cost = resp.json()["cost_micro_cents"]

    assert api_cost == stored_sum, (
        f"GET /v1/usage returned {api_cost} but DB sum is {stored_sum}"
    )


# ──────────────────────── D. Plan isolation ───────────────────────────────────


def test_plan_isolation_free_and_pro(client, db):
    free_plan = _get_or_create_plan(db, PlanCode.free)
    pro_plan = _get_or_create_plan(db, PlanCode.pro)

    _, free_key = _make_tenant(db, free_plan, suffix=f"free-{uuid.uuid4().hex[:6]}")
    _, pro_key = _make_tenant(db, pro_plan, suffix=f"pro-{uuid.uuid4().hex[:6]}")

    free_resp = client.get("/v1/usage", headers=_auth_header(free_key))
    pro_resp = client.get("/v1/usage", headers=_auth_header(pro_key))

    assert free_resp.status_code == 200
    assert pro_resp.status_code == 200

    free_data = free_resp.json()
    pro_data = pro_resp.json()

    assert free_data["plan"] == "free"
    assert pro_data["plan"] == "pro"

    assert free_data["api_calls"]["limit"] == free_plan.api_call_limit
    assert free_data["tokens"]["limit"] == free_plan.token_limit

    assert pro_data["api_calls"]["limit"] == pro_plan.api_call_limit
    assert pro_data["tokens"]["limit"] == pro_plan.token_limit

    # Limits must differ between plans
    assert free_data["api_calls"]["limit"] != pro_data["api_calls"]["limit"]
    assert free_data["tokens"]["limit"] != pro_data["tokens"]["limit"]


# ────────────────── E. Previous-month exclusion ───────────────────────────────


def test_previous_month_events_excluded(client, db):
    """Events with occurred_at in a prior month must NOT appear in the rollup.

    This test manually inserts a past-month row directly into the DB and
    verifies it is excluded from GET /v1/usage.
    """
    plan = _get_or_create_plan(db, PlanCode.free)
    tenant, raw_key = _make_tenant(db, plan)

    # ── Current-month event via normal POST /v1/generate ─────────────────────
    _generate(
        client,
        raw_key,
        {"input_tokens": 10, "output_tokens": 5},
    )

    # ── Previous-month event inserted directly with a past occurred_at ────────
    # We need an idempotency_key row first (FK requirement).
    past_idem = IdempotencyKey(
        tenant_id=tenant.id,
        key=f"past-key-{uuid.uuid4().hex}",
        endpoint="/v1/generate",
        request_hash="past_hash_" + uuid.uuid4().hex,
        state=IdemState.completed,
        response_status=200,
        response_body={"note": "past month seed"},
    )
    db.add(past_idem)
    db.flush()

    # Construct a datetime that is definitely in the previous UTC calendar month.
    now_utc = datetime.now(timezone.utc)
    if now_utc.month == 1:
        past_month_dt = datetime(now_utc.year - 1, 12, 15, 12, 0, 0, tzinfo=timezone.utc)
    else:
        past_month_dt = datetime(now_utc.year, now_utc.month - 1, 15, 12, 0, 0, tzinfo=timezone.utc)

    past_event = UsageEvent(
        tenant_id=tenant.id,
        idempotency_id=past_idem.id,
        type=UsageType.api_call,
        category=None,
        quantity=999,
        unit_price_micro_cents=PRICING.api_call,
        cost_micro_cents=999 * PRICING.api_call,
        occurred_at=past_month_dt,
    )
    db.add(past_event)
    db.commit()

    # ── Assert past event actually has the right occurred_at ─────────────────
    db.refresh(past_event)
    assert past_event.occurred_at.year == past_month_dt.year
    assert past_event.occurred_at.month == past_month_dt.month

    # ── GET /v1/usage must only reflect the current-month event ──────────────
    resp = client.get("/v1/usage", headers=_auth_header(raw_key))
    assert resp.status_code == 200
    data = resp.json()

    # The past-month event contributed 999 api_calls; only 1 current-month call
    # should appear.
    assert data["api_calls"]["used"] == 1, (
        f"Expected 1 current-month api call, got {data['api_calls']['used']}. "
        "Previous-month event may be leaking into the rollup."
    )


# ──────────────────────── F. Authentication ───────────────────────────────────


def test_missing_auth_returns_401(client):
    """No Authorization header → 401, same body as /v1/generate."""
    gen_resp = client.post(
        "/v1/generate",
        json={"input_tokens": 1},
        headers={"Idempotency-Key": "k-1"},
    )
    usage_resp = client.get("/v1/usage")

    assert gen_resp.status_code == 401
    assert usage_resp.status_code == 401
    assert usage_resp.json() == gen_resp.json()


def test_invalid_auth_returns_401(client):
    """Invalid / unknown key → 401, same body as /v1/generate."""
    bad_key = "Bearer totally_invalid_key_xyz"

    gen_resp = client.post(
        "/v1/generate",
        json={"input_tokens": 1},
        headers={"Authorization": bad_key, "Idempotency-Key": "k-2"},
    )
    usage_resp = client.get("/v1/usage", headers={"Authorization": bad_key})

    assert gen_resp.status_code == 401
    assert usage_resp.status_code == 401
    assert usage_resp.json() == gen_resp.json()


# ─────────────── Extra: recomputed cost equals stored events ──────────────────


def test_recomputed_cost_equals_stored_event_costs(db):
    """Service recomputation must equal the stored per-event cost_micro_cents sum.

    This asserts the invariant stated in the spec: recomputed == stored.
    """
    from app.services.quota import get_usage_rollup

    plan = _get_or_create_plan(db, PlanCode.free)
    tenant, raw_key = _make_tenant(db, plan, suffix=f"recomp-{uuid.uuid4().hex[:6]}")

    # Insert usage events directly (no HTTP needed; we own the session).
    idem = IdempotencyKey(
        tenant_id=tenant.id,
        key=f"recomp-idem-{uuid.uuid4().hex}",
        endpoint="/v1/generate",
        request_hash="recomp_hash_" + uuid.uuid4().hex,
        state=IdemState.completed,
        response_status=200,
        response_body={},
    )
    db.add(idem)
    db.flush()

    token_costs = price_tokens(input=300, cached_input=100, output=50, reasoning=25)
    api_cost = price_api_call(1)

    events = [
        UsageEvent(
            tenant_id=tenant.id,
            idempotency_id=idem.id,
            type=UsageType.api_call,
            category=None,
            quantity=1,
            unit_price_micro_cents=PRICING.api_call,
            cost_micro_cents=api_cost,
        ),
        UsageEvent(
            tenant_id=tenant.id,
            idempotency_id=idem.id,
            type=UsageType.tokens,
            category=TokenCategory.input,
            quantity=300,
            unit_price_micro_cents=PRICING.input,
            cost_micro_cents=token_costs["input"],
        ),
        UsageEvent(
            tenant_id=tenant.id,
            idempotency_id=idem.id,
            type=UsageType.tokens,
            category=TokenCategory.cached_input,
            quantity=100,
            unit_price_micro_cents=PRICING.cached_input,
            cost_micro_cents=token_costs["cached_input"],
        ),
        UsageEvent(
            tenant_id=tenant.id,
            idempotency_id=idem.id,
            type=UsageType.tokens,
            category=TokenCategory.output,
            quantity=50,
            unit_price_micro_cents=PRICING.output,
            cost_micro_cents=50 * PRICING.output,
        ),
        UsageEvent(
            tenant_id=tenant.id,
            idempotency_id=idem.id,
            type=UsageType.tokens,
            category=TokenCategory.reasoning,
            quantity=25,
            unit_price_micro_cents=PRICING.reasoning,
            cost_micro_cents=25 * PRICING.reasoning,
        ),
    ]
    db.add_all(events)
    db.commit()

    # Sum stored costs
    month_start = db.execute(
        sa.select(sa.func.date_trunc("month", sa.func.now()))
    ).scalar_one()
    stored_sum = db.scalar(
        sa.select(sa.func.sum(UsageEvent.cost_micro_cents)).where(
            UsageEvent.tenant_id == tenant.id,
            UsageEvent.occurred_at >= month_start,
        )
    ) or 0

    rollup = get_usage_rollup(session=db, tenant=tenant)
    assert rollup["cost_micro_cents"] == stored_sum, (
        f"Recomputed cost {rollup['cost_micro_cents']} != stored sum {stored_sum}"
    )
