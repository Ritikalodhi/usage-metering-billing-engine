"""Quota service — Phase 4.

get_usage_rollup() queries usage_events directly (no rollup cache).
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db.models import Plan, Tenant, TokenCategory, UsageEvent, UsageType
from app.services.cost import price_api_call, price_tokens


def get_usage_rollup(session: Session, tenant: Tenant) -> dict:
    """Return the current-month usage rollup for *tenant*.

    Queries ``usage_events`` directly; does NOT use the ``usage_rollups`` cache
    table (that is Phase 6).

    The period is the UTC calendar month defined by
    ``occurred_at >= date_trunc('month', now())``.

    Cost is recomputed from pricing constants — not blindly summed from
    stored ``cost_micro_cents`` — so the numbers are always consistent.

    Returns a plain dict that maps 1-to-1 onto ``UsageResponse``.
    """
    month_start = sa.func.date_trunc("month", sa.func.now())

    # ── Single aggregation query: group by (type, category) ──────────────────
    rows = session.execute(
        sa.select(
            UsageEvent.type,
            UsageEvent.category,
            sa.func.sum(UsageEvent.quantity).label("qty"),
        )
        .where(
            UsageEvent.tenant_id == tenant.id,
            UsageEvent.occurred_at >= month_start,
        )
        .group_by(UsageEvent.type, UsageEvent.category)
    ).all()

    # ── Accumulate raw token quantities per category ──────────────────────────
    api_calls_qty: int = 0
    token_qty: dict[str, int] = {
        "input": 0,
        "cached_input": 0,
        "output": 0,
        "reasoning": 0,
    }

    for row in rows:
        qty = int(row.qty)
        event_type = row.type.value if hasattr(row.type, "value") else str(row.type)

        if event_type == UsageType.api_call.value:
            api_calls_qty += qty
        elif event_type == UsageType.tokens.value:
            cat = row.category.value if hasattr(row.category, "value") else str(row.category)
            if cat in token_qty:
                token_qty[cat] += qty

    # ── Recompute cost from pricing constants (never sum stored cost_micro_cents)
    api_call_cost = price_api_call(api_calls_qty)
    token_cost_breakdown = price_tokens(
        input=token_qty["input"],
        cached_input=token_qty["cached_input"],
        output=token_qty["output"],
        reasoning=token_qty["reasoning"],
    )
    total_tokens = sum(token_qty.values())
    total_cost_micro_cents = api_call_cost + token_cost_breakdown["total_micro_cents"]

    # ── Load plan limits ──────────────────────────────────────────────────────
    plan = session.scalar(sa.select(Plan).where(Plan.id == tenant.plan_id))
    if plan is None:
        raise ValueError(f"Plan {tenant.plan_id} not found for tenant {tenant.id}")

    plan_code = plan.code.value if hasattr(plan.code, "value") else str(plan.code)

    return {
        "plan": plan_code,
        "period": _current_period_label(session),
        "api_calls": {
            "used": api_calls_qty,
            "limit": plan.api_call_limit,
        },
        "tokens": {
            "used": total_tokens,
            "limit": plan.token_limit,
            "by_category": {
                "input": token_qty["input"],
                "cached_input": token_qty["cached_input"],
                "output": token_qty["output"],
                "reasoning": token_qty["reasoning"],
            },
        },
        "cost_micro_cents": total_cost_micro_cents,
        # cost_usd is computed at the schema boundary; not here
    }


def _current_period_label(session: Session) -> str:
    """Return the current UTC month as a 'YYYY-MM' string via the database clock."""
    result = session.execute(
        sa.select(sa.func.to_char(sa.func.date_trunc("month", sa.func.now()), "YYYY-MM"))
    ).scalar_one()
    return result
