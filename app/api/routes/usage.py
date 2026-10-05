"""GET /v1/usage — Phase 4."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant
from app.api.schemas import UsageResponse
from app.db.models import Tenant
from app.db.session import get_db
from app.services.quota import get_usage_rollup

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get(
    "/usage",
    response_model=UsageResponse,
    responses={
        200: {"description": "Current-month usage rollup for the authenticated tenant"},
        401: {"description": "Unauthorized"},
    },
)
def get_usage(
    tenant: Tenant = Depends(get_current_tenant),
    db: Session = Depends(get_db),
) -> UsageResponse:
    """Return the current-month usage rollup for the authenticated tenant.

    Authentication is handled by ``get_current_tenant``; no Idempotency-Key is
    required.  All quota/cost logic lives in ``get_usage_rollup()``.
    """
    rollup = get_usage_rollup(session=db, tenant=tenant)
    return UsageResponse(**rollup)
