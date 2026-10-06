from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant
from app.api.schemas import CheckoutResponse
from app.db.models import Tenant
from app.db.session import get_db
from app.services.stripe_sync import create_checkout_session

router = APIRouter(prefix="/v1/billing", tags=["billing"])


@router.post(
    "/checkout",
    response_model=CheckoutResponse,
    responses={
        200: {"description": "Checkout session created"},
        401: {"description": "Unauthorized"},
    },
)
def checkout(
    tenant: Tenant = Depends(get_current_tenant),
    db: Session = Depends(get_db),
) -> CheckoutResponse:
    url = create_checkout_session(session=db, tenant=tenant)
    return CheckoutResponse(url=url)
