from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config.pricing import PRICING
from app.db.models import (
    Plan,
    Subscription,
    SubStatus,
    Tenant,
    TokenCategory,
    UsageEvent,
    UsageType,
)
from app.repos.idempotency import claim, complete
from app.services.cost import price_api_call, price_tokens


@dataclass
class MeterResult:
    status_code: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def status(self) -> int:
        return self.status_code


def _get_seconds_to_next_month() -> int:
    now = datetime.now(timezone.utc)
    if now.month == 12:
        next_month = datetime(now.year + 1, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    else:
        next_month = datetime(now.year, now.month + 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return max(1, int((next_month - now).total_seconds()))


def record(
    session: Session,
    tenant_id: uuid.UUID | str,
    key: str,
    endpoint: str,
    request_hash: str,
    request_body: dict[str, Any] | None = None,
) -> MeterResult:
    if isinstance(tenant_id, str):
        tenant_id = uuid.UUID(tenant_id)
    if request_body is None:
        request_body = {}

    try:
        # 1. Idempotency claim
        claim_res = claim(
            session=session,
            tenant_id=tenant_id,
            key=key,
            endpoint=endpoint,
            request_hash=request_hash,
        )

        if claim_res.status == "REPLAY":
            return MeterResult(
                status_code=claim_res.response_status or 200,
                body=claim_res.response_body or {},
                headers={"Idempotency-Replayed": "true"},
            )
        elif claim_res.status == "HASH_MISMATCH":
            return MeterResult(
                status_code=422,
                body={
                    "code": "idempotency_key_reused_with_different_hash",
                    "message": "Idempotency key was previously used with a different request payload.",
                },
            )
        elif claim_res.status == "IN_PROGRESS":
            return MeterResult(
                status_code=409,
                body={
                    "code": "concurrent_request_in_progress",
                    "message": "A request with this idempotency key is currently in progress.",
                },
            )

        # claim_res.status == "OWNED" -> proceed in same transaction

        # 2. SELECT ... FOR NO KEY UPDATE on tenant row to serialize concurrent metering
        # for this tenant without conflicting with the FK checks (FOR KEY SHARE) of concurrent claim() calls
        tenant = session.scalar(
            sa.select(Tenant).where(Tenant.id == tenant_id).with_for_update(key_share=True)
        )
        if tenant is None:
            raise ValueError(f"Tenant {tenant_id} not found")

        # 3. Compute current period usage (calendar UTC month)
        month_start = sa.func.date_trunc("month", sa.func.now())
        usage_query = (
            sa.select(
                UsageEvent.type,
                sa.func.coalesce(sa.func.sum(UsageEvent.quantity), 0).label("total_quantity"),
            )
            .where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.occurred_at >= month_start,
            )
            .group_by(UsageEvent.type)
        )

        usage_rows = session.execute(usage_query).all()
        used_by_type: dict[str, int] = {}
        for row in usage_rows:
            type_str = row.type.value if hasattr(row.type, "value") else str(row.type)
            used_by_type[type_str] = int(row.total_quantity)

        api_calls_used = used_by_type.get("api_call", 0)
        tokens_used = used_by_type.get("tokens", 0)

        # 4. Load tenant plan limits and check subscription status
        plan = session.scalar(sa.select(Plan).where(Plan.id == tenant.plan_id))
        if plan is None:
            raise ValueError(f"Plan {tenant.plan_id} not found")

        api_call_limit = plan.api_call_limit
        token_limit = plan.token_limit

        subscription = session.scalar(
            sa.select(Subscription)
            .where(Subscription.tenant_id == tenant_id)
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )

        bad_statuses = {
            SubStatus.past_due,
            SubStatus.canceled,
            SubStatus.unpaid,
            "past_due",
            "canceled",
            "unpaid",
        }
        if subscription is not None and subscription.status in bad_statuses:
            status_val = (
                subscription.status.value
                if hasattr(subscription.status, "value")
                else str(subscription.status)
            )
            rejection_body = {
                "code": "payment_required",
                "message": f"Subscription status is '{status_val}'.",
            }
            complete(
                session=session,
                claim_id=claim_res.id,
                response_status=402,
                response_body=rejection_body,
            )
            session.commit()
            return MeterResult(status_code=402, body=rejection_body)

        # 5. Check quota headroom (used + requested <= limit)
        requested_api_calls = 1
        input_tokens = int(request_body.get("input_tokens") or 0)
        cached_input_tokens = int(request_body.get("cached_input_tokens") or 0)
        output_tokens = int(request_body.get("output_tokens") or 0)
        reasoning_tokens = int(request_body.get("reasoning_tokens") or 0)
        requested_tokens = (
            input_tokens + cached_input_tokens + output_tokens + reasoning_tokens
        )

        if (api_calls_used + requested_api_calls > api_call_limit) or (
            tokens_used + requested_tokens > token_limit
        ):
            retry_after = _get_seconds_to_next_month()
            rejection_body = {
                "code": "quota_exceeded",
                "message": "Monthly usage limit exceeded.",
                "used": {
                    "api_calls": api_calls_used,
                    "tokens": tokens_used,
                },
                "limit": {
                    "api_calls": api_call_limit,
                    "tokens": token_limit,
                },
                "requested": {
                    "api_calls": requested_api_calls,
                    "tokens": requested_tokens,
                },
            }
            complete(
                session=session,
                claim_id=claim_res.id,
                response_status=429,
                response_body=rejection_body,
            )
            session.commit()
            return MeterResult(
                status_code=429,
                body=rejection_body,
                headers={"Retry-After": str(retry_after)},
            )

        # 6. Request allowed: calculate cost and insert usage events
        api_call_cost = price_api_call(1)
        token_costs = price_tokens(
            input=input_tokens,
            cached_input=cached_input_tokens,
            output=output_tokens,
            reasoning=reasoning_tokens,
        )

        events: list[UsageEvent] = []

        # API call event
        events.append(
            UsageEvent(
                tenant_id=tenant_id,
                idempotency_id=claim_res.id,
                type=UsageType.api_call,
                category=None,
                quantity=1,
                unit_price_micro_cents=PRICING.api_call,
                cost_micro_cents=api_call_cost,
            )
        )

        # Token category events (only non-zero categories)
        if input_tokens > 0:
            events.append(
                UsageEvent(
                    tenant_id=tenant_id,
                    idempotency_id=claim_res.id,
                    type=UsageType.tokens,
                    category=TokenCategory.input,
                    quantity=input_tokens,
                    unit_price_micro_cents=PRICING.input,
                    cost_micro_cents=token_costs["input"],
                )
            )

        if cached_input_tokens > 0:
            events.append(
                UsageEvent(
                    tenant_id=tenant_id,
                    idempotency_id=claim_res.id,
                    type=UsageType.tokens,
                    category=TokenCategory.cached_input,
                    quantity=cached_input_tokens,
                    unit_price_micro_cents=PRICING.cached_input,
                    cost_micro_cents=token_costs["cached_input"],
                )
            )

        if output_tokens > 0:
            events.append(
                UsageEvent(
                    tenant_id=tenant_id,
                    idempotency_id=claim_res.id,
                    type=UsageType.tokens,
                    category=TokenCategory.output,
                    quantity=output_tokens,
                    unit_price_micro_cents=PRICING.output,
                    cost_micro_cents=output_tokens * PRICING.output,
                )
            )

        if reasoning_tokens > 0:
            events.append(
                UsageEvent(
                    tenant_id=tenant_id,
                    idempotency_id=claim_res.id,
                    type=UsageType.tokens,
                    category=TokenCategory.reasoning,
                    quantity=reasoning_tokens,
                    unit_price_micro_cents=PRICING.reasoning,
                    cost_micro_cents=reasoning_tokens * PRICING.reasoning,
                )
            )

        session.add_all(events)
        session.flush()

        usage_event_ids = [event.id for event in events]
        total_cost_micro_cents = api_call_cost + token_costs["total_micro_cents"]

        success_body = {
            "usage_event_ids": usage_event_ids,
            "cost": {
                "api_call_micro_cents": api_call_cost,
                "tokens": {
                    "input": token_costs["input"],
                    "cached_input": token_costs["cached_input"],
                    "output": token_costs["output"],
                    "total_micro_cents": token_costs["total_micro_cents"],
                },
                "total_micro_cents": total_cost_micro_cents,
            },
            "used": {
                "api_calls": api_calls_used + requested_api_calls,
                "tokens": tokens_used + requested_tokens,
            },
            "remaining": {
                "api_calls": api_call_limit - (api_calls_used + requested_api_calls),
                "tokens": token_limit - (tokens_used + requested_tokens),
            },
        }

        # 7. Complete idempotency key
        complete(
            session=session,
            claim_id=claim_res.id,
            response_status=200,
            response_body=success_body,
        )

        # 8. Commit the entire transaction
        session.commit()

        return MeterResult(status_code=200, body=success_body)

    except Exception:
        session.rollback()
        raise
