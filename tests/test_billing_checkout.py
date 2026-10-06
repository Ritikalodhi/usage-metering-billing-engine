from __future__ import annotations

import hashlib
import uuid
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.config.settings import settings
from app.db.models import Plan, PlanCode, Tenant
from app.db.session import SessionLocal
from app.main import app
from app.services.stripe_sync import create_checkout_session


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


def _get_or_create_plan(session, code: PlanCode = PlanCode.free) -> Plan:
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


def _make_tenant(session, stripe_customer_id: str | None = None) -> tuple[Tenant, str]:
    plan = _get_or_create_plan(session)
    suffix = uuid.uuid4().hex[:8]
    raw_key = f"sk_test_{uuid.uuid4().hex}"
    api_key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    tenant = Tenant(
        name=f"test-tenant-billing-{suffix}",
        plan_id=plan.id,
        api_key_hash=api_key_hash,
        stripe_customer_id=stripe_customer_id,
    )
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    return tenant, raw_key


def test_checkout_creates_customer_when_missing(client, db):
    """Tenant without stripe_customer_id gets a customer created and stored,

    and session is created with subscription mode, correct price, and tenant IDs.
    """
    tenant, raw_key = _make_tenant(db, stripe_customer_id=None)
    mock_customer = MagicMock()
    mock_customer.id = f"cus_mock_{uuid.uuid4().hex[:8]}"

    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/pay/cs_test_mock_123"

    with patch("stripe.Customer.create", return_value=mock_customer) as mock_customer_create, \
         patch("stripe.checkout.Session.create", return_value=mock_session) as mock_session_create:

        resp = client.post(
            "/v1/billing/checkout",
            headers={"Authorization": f"Bearer {raw_key}"},
        )

        assert resp.status_code == 200
        # response contains the mocked Checkout Session URL
        assert resp.json() == {"url": "https://checkout.stripe.com/pay/cs_test_mock_123"}

        # customer created with metadata tenant_id
        mock_customer_create.assert_called_once_with(
            metadata={"tenant_id": str(tenant.id)}
        )

        # returned customer ID is stored on the tenant in the database
        db.expire_all()
        updated_tenant = db.scalar(sa.select(Tenant).where(Tenant.id == tenant.id))
        assert updated_tenant.stripe_customer_id == mock_customer.id

        # checkout session created with correct parameters
        mock_session_create.assert_called_once()
        _, kwargs = mock_session_create.call_args
        assert kwargs["customer"] == mock_customer.id
        assert kwargs["client_reference_id"] == str(tenant.id)
        assert kwargs["metadata"] == {"tenant_id": str(tenant.id)}
        assert kwargs["line_items"] == [{"price": settings.STRIPE_PRICE_PRO, "quantity": 1}]
        assert kwargs["mode"] == "subscription"
        assert kwargs["success_url"] == "http://localhost:8000/v1/billing/success"
        assert kwargs["cancel_url"] == "http://localhost:8000/v1/billing/cancel"


def test_checkout_reuses_existing_customer(client, db):
    """Tenant with existing stripe_customer_id reuses it and does NOT create another customer."""
    existing_cus_id = f"cus_existing_{uuid.uuid4().hex[:8]}"
    tenant, raw_key = _make_tenant(db, stripe_customer_id=existing_cus_id)

    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/pay/cs_test_existing_456"

    with patch("stripe.Customer.create") as mock_customer_create, \
         patch("stripe.checkout.Session.create", return_value=mock_session) as mock_session_create:

        resp = client.post(
            "/v1/billing/checkout",
            headers={"Authorization": f"Bearer {raw_key}"},
        )

        assert resp.status_code == 200
        # response contains the mocked Checkout Session URL
        assert resp.json() == {"url": "https://checkout.stripe.com/pay/cs_test_existing_456"}

        # does NOT create another customer
        mock_customer_create.assert_not_called()

        # checkout session created with existing customer id
        mock_session_create.assert_called_once()
        _, kwargs = mock_session_create.call_args
        assert kwargs["customer"] == existing_cus_id
        assert kwargs["client_reference_id"] == str(tenant.id)
        assert kwargs["metadata"] == {"tenant_id": str(tenant.id)}
        assert kwargs["line_items"] == [{"price": settings.STRIPE_PRICE_PRO, "quantity": 1}]
        assert kwargs["mode"] == "subscription"

        # customer id unchanged in db
        db.expire_all()
        persisted_tenant = db.scalar(sa.select(Tenant).where(Tenant.id == tenant.id))
        assert persisted_tenant.stripe_customer_id == existing_cus_id


def test_checkout_unauthorized(client):
    """Unauthenticated calls are rejected with 401."""
    with patch("stripe.Customer.create") as mock_cus, \
         patch("stripe.checkout.Session.create") as mock_ses:

        resp = client.post("/v1/billing/checkout")
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Unauthorized"}

        resp_invalid = client.post(
            "/v1/billing/checkout",
            headers={"Authorization": "Bearer not_a_real_token"},
        )
        assert resp_invalid.status_code == 401
        assert resp_invalid.json() == {"detail": "Unauthorized"}

        mock_cus.assert_not_called()
        mock_ses.assert_not_called()


def test_create_checkout_session_direct(db):
    """Direct invocation of create_checkout_session service helper."""
    tenant, _ = _make_tenant(db, stripe_customer_id=None)

    mock_customer = MagicMock()
    mock_customer.id = f"cus_direct_{uuid.uuid4().hex[:8]}"

    mock_session = MagicMock()
    mock_session.url = "https://checkout.stripe.com/pay/cs_direct_url"

    with patch("stripe.Customer.create", return_value=mock_customer) as mock_customer_create, \
         patch("stripe.checkout.Session.create", return_value=mock_session) as mock_session_create:

        url = create_checkout_session(session=db, tenant=tenant)

        assert url == "https://checkout.stripe.com/pay/cs_direct_url"
        mock_customer_create.assert_called_once_with(
            metadata={"tenant_id": str(tenant.id)}
        )
        assert tenant.stripe_customer_id == mock_customer.id

        mock_session_create.assert_called_once()
        _, kwargs = mock_session_create.call_args
        assert kwargs["customer"] == mock_customer.id
        assert kwargs["client_reference_id"] == str(tenant.id)
        assert kwargs["metadata"] == {"tenant_id": str(tenant.id)}
        assert kwargs["line_items"] == [{"price": settings.STRIPE_PRICE_PRO, "quantity": 1}]
        assert kwargs["mode"] == "subscription"
