import hashlib
import json
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.db.models import (
    IdempotencyKey,
    Plan,
    PlanCode,
    Subscription,
    SubStatus,
    Tenant,
    UsageEvent,
    UsageType,
)
from app.db.session import SessionLocal
from app.main import app


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def api_tenant(db_session):
    plan = db_session.scalar(sa.select(Plan).where(Plan.code == PlanCode.free))
    if not plan:
        plan = Plan(
            code=PlanCode.free,
            display_name="Free",
            api_call_limit=1000,
            token_limit=100000,
        )
        db_session.add(plan)
        db_session.commit()

    raw_key = f"sk_test_{uuid.uuid4().hex}"
    api_key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    tenant = Tenant(
        name=f"tenant-api-{uuid.uuid4().hex[:8]}",
        plan_id=plan.id,
        api_key_hash=api_key_hash,
    )
    db_session.add(tenant)
    db_session.commit()

    yield tenant, raw_key

    cleanup_session = SessionLocal()
    try:
        cleanup_session.execute(sa.delete(Tenant).where(Tenant.id == tenant.id))
        cleanup_session.commit()
    finally:
        cleanup_session.close()


def test_normal_request_returns_200_with_expected_cost(client, api_tenant):
    tenant, raw_key = api_tenant
    idem_key = f"key-{uuid.uuid4().hex}"

    payload = {
        "input_tokens": 100,
        "cached_input_tokens": 50,
        "output_tokens": 20,
        "reasoning_tokens": 10,
    }

    response = client.post(
        "/v1/generate",
        json=payload,
        headers={
            "Authorization": f"Bearer {raw_key}",
            "Idempotency-Key": idem_key,
        },
    )

    assert response.status_code == 200
    data = response.json()

    # Cost breakdown checks:
    # API call: 200 micro cents
    # input: 100 * 300 = 30,000
    # cached_input: 50 * 30 = 1,500
    # output: 20 * 1500 = 30,000
    # reasoning: 10 * 1500 = 15,000
    # tokens total = 76,500
    # grand total = 76,700
    assert data["cost"]["api_call_micro_cents"] == 200
    assert data["cost"]["tokens"]["input"] == 30000
    assert data["cost"]["tokens"]["cached_input"] == 1500
    assert data["cost"]["tokens"]["output"] == 45000  # output + reasoning folded into output token cost
    assert data["cost"]["tokens"]["total_micro_cents"] == 76500
    assert data["cost"]["total_micro_cents"] == 76700

    assert data["used"]["api_calls"] == 1
    assert data["used"]["tokens"] == 180  # 100 + 50 + 20 + 10
    assert data["remaining"]["api_calls"] == 999
    assert data["remaining"]["tokens"] == 99820
    assert len(data["usage_event_ids"]) == 5


def test_idempotency_same_key_returns_identical_body_and_no_new_usage_events(
    client, api_tenant, db_session
):
    tenant, raw_key = api_tenant
    idem_key = f"key-{uuid.uuid4().hex}"

    payload = {
        "input_tokens": 200,
        "cached_input_tokens": 0,
        "output_tokens": 50,
        "reasoning_tokens": 0,
    }

    headers = {
        "Authorization": f"Bearer {raw_key}",
        "Idempotency-Key": idem_key,
    }

    # 1. First request
    resp1 = client.post("/v1/generate", json=payload, headers=headers)
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert "Idempotency-Replayed" not in resp1.headers

    # Count usage events directly from DB
    count1 = db_session.scalar(
        sa.select(sa.func.count(UsageEvent.id)).where(UsageEvent.tenant_id == tenant.id)
    )
    assert count1 > 0

    # 2. Second request with identical key and payload
    resp2 = client.post("/v1/generate", json=payload, headers=headers)
    assert resp2.status_code == 200
    body2 = resp2.json()

    assert body1 == body2
    assert resp2.headers.get("Idempotency-Replayed") == "true"

    # Count usage events again - must be unchanged
    count2 = db_session.scalar(
        sa.select(sa.func.count(UsageEvent.id)).where(UsageEvent.tenant_id == tenant.id)
    )
    assert count2 == count1


def test_same_key_different_body_returns_422(client, api_tenant):
    tenant, raw_key = api_tenant
    idem_key = f"key-{uuid.uuid4().hex}"

    headers = {
        "Authorization": f"Bearer {raw_key}",
        "Idempotency-Key": idem_key,
    }

    # Request 1
    resp1 = client.post(
        "/v1/generate",
        json={"input_tokens": 100, "output_tokens": 50},
        headers=headers,
    )
    assert resp1.status_code == 200

    # Request 2 with same key but different body
    resp2 = client.post(
        "/v1/generate",
        json={"input_tokens": 200, "output_tokens": 50},
        headers=headers,
    )
    assert resp2.status_code == 422
    data = resp2.json()
    assert data.get("code") == "idempotency_key_reused_with_different_hash"


def test_same_body_different_key_ordering_returns_replay_not_422(client, api_tenant):
    tenant, raw_key = api_tenant
    idem_key = f"key-{uuid.uuid4().hex}"

    headers = {
        "Authorization": f"Bearer {raw_key}",
        "Idempotency-Key": idem_key,
        "Content-Type": "application/json",
    }

    # First payload json string with key ordering A
    body_a = json.dumps({"input_tokens": 100, "output_tokens": 50, "cached_input_tokens": 0, "reasoning_tokens": 0})
    resp1 = client.post("/v1/generate", content=body_a, headers=headers)
    assert resp1.status_code == 200

    # Second payload json string with key ordering B (different order in raw string)
    body_b = json.dumps({"reasoning_tokens": 0, "output_tokens": 50, "input_tokens": 100, "cached_input_tokens": 0})
    resp2 = client.post("/v1/generate", content=body_b, headers=headers)
    assert resp2.status_code == 200
    assert resp2.headers.get("Idempotency-Replayed") == "true"
    assert resp1.json() == resp2.json()


def test_missing_or_invalid_idempotency_key_returns_400(client, api_tenant):
    tenant, raw_key = api_tenant

    # Case 1: Missing Idempotency-Key header
    resp1 = client.post(
        "/v1/generate",
        json={"input_tokens": 10},
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp1.status_code == 400

    # Case 2: Empty Idempotency-Key header
    resp2 = client.post(
        "/v1/generate",
        json={"input_tokens": 10},
        headers={"Authorization": f"Bearer {raw_key}", "Idempotency-Key": ""},
    )
    assert resp2.status_code == 400

    # Case 3: Whitespace only Idempotency-Key header
    resp3 = client.post(
        "/v1/generate",
        json={"input_tokens": 10},
        headers={"Authorization": f"Bearer {raw_key}", "Idempotency-Key": "   "},
    )
    assert resp3.status_code == 400

    # Case 4: Idempotency-Key > 255 chars
    long_key = "k" * 256
    resp4 = client.post(
        "/v1/generate",
        json={"input_tokens": 10},
        headers={"Authorization": f"Bearer {raw_key}", "Idempotency-Key": long_key},
    )
    assert resp4.status_code == 400


def test_missing_malformed_or_unknown_api_key_returns_identical_401(client):
    headers_no_auth = {"Idempotency-Key": "key-1"}
    resp_no_auth = client.post("/v1/generate", json={"input_tokens": 10}, headers=headers_no_auth)

    headers_wrong_scheme = {"Authorization": "Basic dXNlcjpwYXNz", "Idempotency-Key": "key-1"}
    resp_wrong_scheme = client.post("/v1/generate", json={"input_tokens": 10}, headers=headers_wrong_scheme)

    headers_empty_bearer = {"Authorization": "Bearer ", "Idempotency-Key": "key-1"}
    resp_empty_bearer = client.post("/v1/generate", json={"input_tokens": 10}, headers=headers_empty_bearer)

    headers_unknown_key = {"Authorization": "Bearer non_existent_key_12345", "Idempotency-Key": "key-1"}
    resp_unknown_key = client.post("/v1/generate", json={"input_tokens": 10}, headers=headers_unknown_key)

    assert resp_no_auth.status_code == 401
    assert resp_wrong_scheme.status_code == 401
    assert resp_empty_bearer.status_code == 401
    assert resp_unknown_key.status_code == 401

    body_no_auth = resp_no_auth.json()
    assert resp_wrong_scheme.json() == body_no_auth
    assert resp_empty_bearer.json() == body_no_auth
    assert resp_unknown_key.json() == body_no_auth


def test_negative_or_oversized_token_count_returns_422(client, api_tenant):
    tenant, raw_key = api_tenant
    headers = {
        "Authorization": f"Bearer {raw_key}",
        "Idempotency-Key": f"key-{uuid.uuid4().hex}",
    }

    # Negative tokens
    resp_neg = client.post("/v1/generate", json={"input_tokens": -1}, headers=headers)
    assert resp_neg.status_code == 422
    assert resp_neg.status_code != 500

    # Oversized tokens (> 10_000_000)
    resp_over = client.post("/v1/generate", json={"output_tokens": 10_000_001}, headers=headers)
    assert resp_over.status_code == 422
    assert resp_over.status_code != 500

    # Extra disallowed field
    resp_extra = client.post("/v1/generate", json={"unknown_param": 100}, headers=headers)
    assert resp_extra.status_code == 422
    assert resp_extra.status_code != 500

    # Non-integer type
    resp_type = client.post("/v1/generate", json={"input_tokens": "abc"}, headers=headers)
    assert resp_type.status_code == 422
    assert resp_type.status_code != 500


def test_boundary_walk_over_http(client, api_tenant, db_session):
    tenant, raw_key = api_tenant

    # Seed 999 api_call usage events for this tenant in current month
    idem_seed = IdempotencyKey(
        tenant_id=tenant.id,
        key=f"seed-key-{uuid.uuid4().hex}",
        endpoint="/v1/generate",
        request_hash="seed_hash",
        response_status=200,
    )
    db_session.add(idem_seed)
    db_session.flush()

    seed_event = UsageEvent(
        tenant_id=tenant.id,
        idempotency_id=idem_seed.id,
        type=UsageType.api_call,
        category=None,
        quantity=999,
        unit_price_micro_cents=200,
        cost_micro_cents=999 * 200,
    )
    db_session.add(seed_event)
    db_session.commit()

    # 1000th call should succeed with 200
    key_1000 = f"key-1000-{uuid.uuid4().hex}"
    resp_1000 = client.post(
        "/v1/generate",
        json={"input_tokens": 10},
        headers={"Authorization": f"Bearer {raw_key}", "Idempotency-Key": key_1000},
    )
    assert resp_1000.status_code == 200
    assert resp_1000.json()["used"]["api_calls"] == 1000
    assert resp_1000.json()["remaining"]["api_calls"] == 0

    # 1001st call should fail with 429 and Retry-After header
    key_1001 = f"key-1001-{uuid.uuid4().hex}"
    resp_1001 = client.post(
        "/v1/generate",
        json={"input_tokens": 10},
        headers={"Authorization": f"Bearer {raw_key}", "Idempotency-Key": key_1001},
    )
    assert resp_1001.status_code == 429
    assert "Retry-After" in resp_1001.headers
    retry_after_val = int(resp_1001.headers["Retry-After"])
    assert retry_after_val > 0
    body_429 = resp_1001.json()
    assert body_429["code"] == "quota_exceeded"

    # Retrying 1001st with same key should return identical 429 replay
    resp_1001_retry = client.post(
        "/v1/generate",
        json={"input_tokens": 10},
        headers={"Authorization": f"Bearer {raw_key}", "Idempotency-Key": key_1001},
    )
    assert resp_1001_retry.status_code == 429
    assert resp_1001_retry.headers.get("Idempotency-Replayed") == "true"
    assert resp_1001_retry.json() == body_429


def test_past_due_tenant_returns_402(client, api_tenant, db_session):
    tenant, raw_key = api_tenant

    # Add past_due subscription
    sub = Subscription(
        tenant_id=tenant.id,
        plan_id=tenant.plan_id,
        stripe_subscription_id=f"sub_test_{uuid.uuid4().hex[:12]}",
        status=SubStatus.past_due,
        last_event_at=sa.func.now(),
        current_period_start=sa.func.now(),
        current_period_end=sa.func.now(),
    )
    db_session.add(sub)
    db_session.commit()

    resp = client.post(
        "/v1/generate",
        json={"input_tokens": 10},
        headers={
            "Authorization": f"Bearer {raw_key}",
            "Idempotency-Key": f"key-{uuid.uuid4().hex}",
        },
    )
    assert resp.status_code == 402
    assert resp.json().get("code") == "payment_required"


def test_unhandled_exception_returns_500_with_request_id(api_tenant, monkeypatch):
    tenant, raw_key = api_tenant

    def mock_record(*args, **kwargs):
        raise RuntimeError("Unexpected internal crash with sensitive stack info")

    monkeypatch.setattr("app.api.routes.generate.record", mock_record)

    client_no_raise = TestClient(app, raise_server_exceptions=False)
    resp = client_no_raise.post(
        "/v1/generate",
        json={"input_tokens": 10},
        headers={
            "Authorization": f"Bearer {raw_key}",
            "Idempotency-Key": f"key-{uuid.uuid4().hex}",
        },
    )
    assert resp.status_code == 500
    data = resp.json()
    assert data["code"] == "internal_server_error"
    assert data["message"] == "An unexpected error occurred."
    assert "request_id" in data
    assert "sensitive stack info" not in resp.text

