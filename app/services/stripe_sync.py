from __future__ import annotations

import stripe
from sqlalchemy.orm import Session

from app.config.settings import settings
from app.db.models import Tenant


def create_checkout_session(session: Session, tenant: Tenant) -> str:
    stripe.api_key = settings.STRIPE_API_KEY

    if not tenant.stripe_customer_id:
        customer = stripe.Customer.create(
            metadata={"tenant_id": str(tenant.id)},
        )
        tenant.stripe_customer_id = customer.id
        session.commit()

    # Note: success_url and cancel_url are placeholders for local development.
    checkout_session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": settings.STRIPE_PRICE_PRO, "quantity": 1}],
        customer=tenant.stripe_customer_id,
        client_reference_id=str(tenant.id),
        metadata={"tenant_id": str(tenant.id)},
        success_url="http://localhost:8000/v1/billing/success",
        cancel_url="http://localhost:8000/v1/billing/cancel",
    )
    return checkout_session.url
