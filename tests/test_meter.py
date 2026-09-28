import hashlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.db.models import (
    IdempotencyKey,
    Plan,
    PlanCode,
    Subscription,
    SubStatus,
    Tenant,
    TokenCategory,
    UsageEvent,
    UsageType,
)
from app.db.session import SessionLocal
from app.services.meter import record


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def free_tenant(db_session):
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

    tenant = Tenant(
        name=f"tenant-meter-{uuid.uuid4().hex[:8]}",
        plan_id=plan.id,
        api_key_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
    )
    db_session.add(tenant)
    db_session.commit()
    return tenant


def test_normal_request_metering(db_session, free_tenant):
    key = f"key-normal-{uuid.uuid4().hex}"
    request_hash = "hash_normal_test"
    request_body = {
        "input_tokens": 100,
        "cached_input_tokens": 50,
        "output_tokens": 20,
        "reasoning_tokens": 10,
    }

    result = record(
        session=db_session,
        tenant_id=free_tenant.id,
        key=key,
        endpoint="/v1/generate",
        request_hash=request_hash,
        request_body=request_body,
    )

    assert result.status_code == 200
    assert "usage_event_ids" in result.body
    assert len(result.body["usage_event_ids"]) == 5  # 1 api_call + 4 token categories

    # Cost breakdown verification:
    # 1 api_call = 200 micro cents
    # 100 input * 300 = 30000
    # 50 cached * 30 = 1500
    # 20 output * 1500 = 30000
    # 10 reasoning * 1500 = 15000
    # Total tokens = 76500
    # Total micro cents = 76700
    assert result.body["cost"]["api_call_micro_cents"] == 200
    assert result.body["cost"]["tokens"]["input"] == 30000
    assert result.body["cost"]["tokens"]["cached_input"] == 1500
    assert result.body["cost"]["tokens"]["output"] == 45000  # (20+10)*1500 in calculator
    assert result.body["cost"]["tokens"]["total_micro_cents"] == 76500
    assert result.body["cost"]["total_micro_cents"] == 76700

    # Query DB directly to verify ledger rows
    events = (
        db_session.scalars(
            sa.select(UsageEvent).where(UsageEvent.tenant_id == free_tenant.id)
        )
        .all()
    )
    assert len(events) == 5

    api_call_event = next(e for e in events if e.type == UsageType.api_call)
    assert api_call_event.quantity == 1
    assert api_call_event.unit_price_micro_cents == 200
    assert api_call_event.cost_micro_cents == 200
    assert api_call_event.category is None

    token_events = [e for e in events if e.type == UsageType.tokens]
    categories = {e.category: (e.quantity, e.cost_micro_cents) for e in token_events}
    assert categories[TokenCategory.input] == (100, 30000)
    assert categories[TokenCategory.cached_input] == (50, 1500)
    assert categories[TokenCategory.output] == (20, 30000)
    assert categories[TokenCategory.reasoning] == (10, 15000)


def test_idempotency_replay_metering(db_session, free_tenant):
    key = f"key-replay-{uuid.uuid4().hex}"
    request_hash = "hash_replay_test"
    request_body = {
        "input_tokens": 50,
        "output_tokens": 50,
    }

    # 1. First record call
    res1 = record(
        session=db_session,
        tenant_id=free_tenant.id,
        key=key,
        endpoint="/v1/generate",
        request_hash=request_hash,
        request_body=request_body,
    )
    assert res1.status_code == 200

    # Record event count in DB
    events_count_before = db_session.scalar(
        sa.select(sa.func.count(UsageEvent.id)).where(UsageEvent.tenant_id == free_tenant.id)
    )

    # 2. Replay with identical key & hash in a fresh DB session
    session2 = SessionLocal()
    try:
        res2 = record(
            session=session2,
            tenant_id=free_tenant.id,
            key=key,
            endpoint="/v1/generate",
            request_hash=request_hash,
            request_body=request_body,
        )
        assert res2.status_code == 200
        assert res2.headers.get("Idempotency-Replayed") == "true"
        assert res2.body == res1.body

        # Assert no new rows were inserted in DB
        events_count_after = session2.scalar(
            sa.select(sa.func.count(UsageEvent.id)).where(UsageEvent.tenant_id == free_tenant.id)
        )
        assert events_count_after == events_count_before
    finally:
        session2.close()


def test_boundary_walk(db_session, free_tenant):
    # Free plan limit: 1000 api calls.
    # Seed usage to exactly limit - 1 = 999 calls.
    idem_seed = IdempotencyKey(
        tenant_id=free_tenant.id,
        key=f"seed-key-{uuid.uuid4().hex}",
        endpoint="/v1/generate",
        request_hash="seed_hash",
        response_status=200,
    )
    db_session.add(idem_seed)
    db_session.flush()

    seed_event = UsageEvent(
        tenant_id=free_tenant.id,
        idempotency_id=idem_seed.id,
        type=UsageType.api_call,
        category=None,
        quantity=999,
        unit_price_micro_cents=200,
        cost_micro_cents=999 * 200,
    )
    db_session.add(seed_event)
    db_session.commit()

    # 1. 1-unit request at 999 -> succeeds, usage now at 1000 (limit reached)
    key_hit_limit = f"key-hit-limit-{uuid.uuid4().hex}"
    res_hit = record(
        session=db_session,
        tenant_id=free_tenant.id,
        key=key_hit_limit,
        endpoint="/v1/generate",
        request_hash="hash_hit",
        request_body={},
    )
    assert res_hit.status_code == 200
    assert res_hit.body["used"]["api_calls"] == 1000
    assert res_hit.body["remaining"]["api_calls"] == 0

    # Total api calls in DB now = 1000
    total_calls = db_session.scalar(
        sa.select(sa.func.sum(UsageEvent.quantity)).where(
            UsageEvent.tenant_id == free_tenant.id,
            UsageEvent.type == UsageType.api_call,
        )
    )
    assert total_calls == 1000

    # 2. Next 1-unit request at 1000 -> 429 quota_exceeded
    key_over_limit = f"key-over-limit-{uuid.uuid4().hex}"
    session_over = SessionLocal()
    try:
        res_over = record(
            session=session_over,
            tenant_id=free_tenant.id,
            key=key_over_limit,
            endpoint="/v1/generate",
            request_hash="hash_over",
            request_body={},
        )
        assert res_over.status_code == 429
        assert res_over.body["code"] == "quota_exceeded"
        assert "Retry-After" in res_over.headers

        # Usage in DB remains 1000 (not incremented)
        total_calls_after_reject = session_over.scalar(
            sa.select(sa.func.sum(UsageEvent.quantity)).where(
                UsageEvent.tenant_id == free_tenant.id,
                UsageEvent.type == UsageType.api_call,
            )
        )
        assert total_calls_after_reject == 1000

        # 3. Retry that exact rejected request with same idempotency key -> identical 429 replay
        session_retry = SessionLocal()
        try:
            res_retry = record(
                session=session_retry,
                tenant_id=free_tenant.id,
                key=key_over_limit,
                endpoint="/v1/generate",
                request_hash="hash_over",
                request_body={},
            )
            assert res_retry.status_code == 429
            assert res_retry.headers.get("Idempotency-Replayed") == "true"
            assert res_retry.body == res_over.body

            # Usage in DB is still 1000 (neither decremented nor incremented)
            total_calls_after_retry = session_retry.scalar(
                sa.select(sa.func.sum(UsageEvent.quantity)).where(
                    UsageEvent.tenant_id == free_tenant.id,
                    UsageEvent.type == UsageType.api_call,
                )
            )
            assert total_calls_after_retry == 1000
        finally:
            session_retry.close()
    finally:
        session_over.close()


def test_rejection_in_full_no_partial_metering(db_session, free_tenant):
    # Free token limit = 100,000.
    # Seed token usage to limit - 3 = 99,997.
    idem_seed = IdempotencyKey(
        tenant_id=free_tenant.id,
        key=f"seed-key-{uuid.uuid4().hex}",
        endpoint="/v1/generate",
        request_hash="seed_hash",
        response_status=200,
    )
    db_session.add(idem_seed)
    db_session.flush()

    seed_tokens = UsageEvent(
        tenant_id=free_tenant.id,
        idempotency_id=idem_seed.id,
        type=UsageType.tokens,
        category=TokenCategory.input,
        quantity=99997,
        unit_price_micro_cents=300,
        cost_micro_cents=99997 * 300,
    )
    db_session.add(seed_tokens)
    db_session.commit()

    # Request 5 tokens when only 3 remain -> must be rejected in full (no partial 3-token metering)
    key = f"key-jump-{uuid.uuid4().hex}"
    session_req = SessionLocal()
    try:
        res = record(
            session=session_req,
            tenant_id=free_tenant.id,
            key=key,
            endpoint="/v1/generate",
            request_hash="hash_jump",
            request_body={"input_tokens": 5},
        )
        assert res.status_code == 429
        assert res.body["code"] == "quota_exceeded"

        # Usage must remain exactly 99997 (no partial increment to 100000 or 100002)
        total_tokens = session_req.scalar(
            sa.select(sa.func.sum(UsageEvent.quantity)).where(
                UsageEvent.tenant_id == free_tenant.id,
                UsageEvent.type == UsageType.tokens,
            )
        )
        assert total_tokens == 99997
    finally:
        session_req.close()


def test_subscription_past_due(db_session, free_tenant):
    # Create subscription in past_due status
    plan = db_session.scalar(sa.select(Plan).where(Plan.code == PlanCode.pro))
    sub = Subscription(
        tenant_id=free_tenant.id,
        plan_id=plan.id if plan else free_tenant.plan_id,
        stripe_subscription_id=f"sub_{uuid.uuid4().hex[:12]}",
        status=SubStatus.past_due,
        last_event_at=datetime.now(timezone.utc),
    )
    db_session.add(sub)
    db_session.commit()

    # Even with 0 usage, request must return 402 payment_required
    key = f"key-past-due-{uuid.uuid4().hex}"
    session_req = SessionLocal()
    try:
        res = record(
            session=session_req,
            tenant_id=free_tenant.id,
            key=key,
            endpoint="/v1/generate",
            request_hash="hash_past_due",
            request_body={"input_tokens": 10},
        )
        assert res.status_code == 402
        assert res.body["code"] == "payment_required"

        # No usage event recorded
        usage_count = session_req.scalar(
            sa.select(sa.func.count(UsageEvent.id)).where(UsageEvent.tenant_id == free_tenant.id)
        )
        assert usage_count == 0
    finally:
        session_req.close()


def test_concurrency_race_at_limit(free_tenant):
    # Seed usage to limit - 1 = 999 api_calls
    session_seed = SessionLocal()
    try:
        idem_seed = IdempotencyKey(
            tenant_id=free_tenant.id,
            key=f"seed-key-{uuid.uuid4().hex}",
            endpoint="/v1/generate",
            request_hash="seed_hash",
            response_status=200,
        )
        session_seed.add(idem_seed)
        session_seed.flush()

        seed_event = UsageEvent(
            tenant_id=free_tenant.id,
            idempotency_id=idem_seed.id,
            type=UsageType.api_call,
            category=None,
            quantity=999,
            unit_price_micro_cents=200,
            cost_micro_cents=999 * 200,
        )
        session_seed.add(seed_event)
        session_seed.commit()
    finally:
        session_seed.close()

    # Two worker threads, separate sessions, same tenant sitting at 999/1000 limit.
    # Both request 1 unit simultaneously with different idempotency keys.
    barrier = threading.Barrier(2)
    keys = [f"race-key-1-{uuid.uuid4().hex}", f"race-key-2-{uuid.uuid4().hex}"]

    def worker_action(worker_key: str):
        worker_session = SessionLocal()
        try:
            # Force threads to reach and start record() simultaneously
            barrier.wait()
            return record(
                session=worker_session,
                tenant_id=free_tenant.id,
                key=worker_key,
                endpoint="/v1/generate",
                request_hash=f"hash_{worker_key}",
                request_body={},
            )
        finally:
            worker_session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker_action, keys[0])
        f2 = executor.submit(worker_action, keys[1])
        r1, r2 = f1.result(), f2.result()

    statuses = [r1.status_code, r2.status_code]
    assert 200 in statuses, f"Expected one 200 success, got {statuses}"
    assert 429 in statuses, f"Expected one 429 quota_exceeded, got {statuses}"

    # Verify total usage in DB hit exactly 1000, not 1001 (serialized by SELECT ... FOR UPDATE)
    verify_session = SessionLocal()
    try:
        total_calls = verify_session.scalar(
            sa.select(sa.func.sum(UsageEvent.quantity)).where(
                UsageEvent.tenant_id == free_tenant.id,
                UsageEvent.type == UsageType.api_call,
            )
        )
        assert total_calls == 1000
    finally:
        verify_session.close()
