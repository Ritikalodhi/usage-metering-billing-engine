import hashlib
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.config.plans import FREE, PLANS, PRO, Plan
from app.config.pricing import PRICING
from app.services.cost import price_tokens
from app.db.models import (
    IdemState,
    IdempotencyKey,
    Plan as PlanModel,
    PlanCode,
    Subscription,
    SubStatus,
    Tenant,
    TokenCategory,
    UsageEvent,
    UsageType,
)
from app.db.session import SessionLocal


def test_plans_config():
    assert FREE.code == "free"
    assert FREE.display_name == "Free"
    assert FREE.api_call_limit == 1_000
    assert FREE.token_limit == 100_000

    assert PRO.code == "pro"
    assert PRO.display_name == "Pro"
    assert PRO.api_call_limit == 50_000
    assert PRO.token_limit == 5_000_000

    assert PLANS["free"] == FREE
    assert PLANS["pro"] == PRO


def test_pricing_config_and_calculation():
    assert PRICING.input == 300
    assert PRICING.cached_input == 30
    assert PRICING.output == 1500
    assert PRICING.reasoning == 1500
    assert PRICING.api_call == 200

    # Test formula: in*300 + cached*30 + (out + reasoning)*1500
    # e.g., 1000 input = 300_000
    # 500 cached_input = 15_000
    # 200 output + 100 reasoning = 300 * 1500 = 450_000
    # total = 765_000
    result = price_tokens(input=1000, cached_input=500, output=200, reasoning=100)
    assert result["input"] == 300_000
    assert result["cached_input"] == 15_000
    assert result["output"] == 450_000
    assert result["total_micro_cents"] == 765_000


def test_db_models_and_constraints():
    session = SessionLocal()
    try:
        # 1. Plans seeded
        free_plan = session.scalar(sa.select(PlanModel).where(PlanModel.code == PlanCode.free))
        pro_plan = session.scalar(sa.select(PlanModel).where(PlanModel.code == PlanCode.pro))
        assert free_plan is not None
        assert pro_plan is not None
        assert free_plan.api_call_limit == 1000
        assert pro_plan.token_limit == 5000000

        # Create a test tenant
        test_tenant = Tenant(
            name=f"test-tenant-{uuid.uuid4().hex[:8]}",
            plan_id=free_plan.id,
            api_key_hash=hashlib.sha256(b"test_key").hexdigest(),
        )
        session.add(test_tenant)
        session.commit()

        # 2. Test Partial Unique Index on Subscriptions (one active sub per tenant)
        sub1 = Subscription(
            tenant_id=test_tenant.id,
            plan_id=pro_plan.id,
            stripe_subscription_id=f"sub_{uuid.uuid4().hex[:12]}",
            status=SubStatus.active,
            last_event_at=datetime.now(timezone.utc),
        )
        session.add(sub1)
        session.commit()

        # Second active sub for same tenant must fail
        sub2 = Subscription(
            tenant_id=test_tenant.id,
            plan_id=pro_plan.id,
            stripe_subscription_id=f"sub_{uuid.uuid4().hex[:12]}",
            status=SubStatus.active,
            last_event_at=datetime.now(timezone.utc),
        )
        session.add(sub2)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        # Canceled sub for same tenant should be allowed (partial index allows canceled)
        sub_canceled = Subscription(
            tenant_id=test_tenant.id,
            plan_id=pro_plan.id,
            stripe_subscription_id=f"sub_{uuid.uuid4().hex[:12]}",
            status=SubStatus.canceled,
            last_event_at=datetime.now(timezone.utc),
        )
        session.add(sub_canceled)
        session.commit()

        # 3. Test Composite Unique Constraint on IdempotencyKeys (tenant_id, key)
        idem1 = IdempotencyKey(
            tenant_id=test_tenant.id,
            key="idem-key-1",
            endpoint="/v1/generate",
            request_hash="hash1",
            state=IdemState.in_progress,
        )
        session.add(idem1)
        session.commit()

        idem2_duplicate = IdempotencyKey(
            tenant_id=test_tenant.id,
            key="idem-key-1",
            endpoint="/v1/generate",
            request_hash="hash1",
            state=IdemState.in_progress,
        )
        session.add(idem2_duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        # 4. Test Check Constraint on UsageEvents (category matches type)
        # Re-fetch or re-add idem1 since rollback cleared it
        idem = IdempotencyKey(
            tenant_id=test_tenant.id,
            key="idem-key-test",
            endpoint="/v1/generate",
            request_hash="hash_test",
            state=IdemState.completed,
        )
        session.add(idem)
        session.commit()

        # Valid usage events
        event_call = UsageEvent(
            tenant_id=test_tenant.id,
            idempotency_id=idem.id,
            type=UsageType.api_call,
            category=None,
            quantity=1,
            unit_price_micro_cents=200,
            cost_micro_cents=200,
        )
        event_tokens = UsageEvent(
            tenant_id=test_tenant.id,
            idempotency_id=idem.id,
            type=UsageType.tokens,
            category=TokenCategory.input,
            quantity=100,
            unit_price_micro_cents=300,
            cost_micro_cents=30000,
        )
        session.add_all([event_call, event_tokens])
        session.commit()

        # Invalid usage event: type=api_call but category is NOT null
        invalid_event = UsageEvent(
            tenant_id=test_tenant.id,
            idempotency_id=idem.id,
            type=UsageType.api_call,
            category=TokenCategory.input,
            quantity=1,
            unit_price_micro_cents=200,
            cost_micro_cents=200,
        )
        session.add(invalid_event)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    finally:
        session.close()
