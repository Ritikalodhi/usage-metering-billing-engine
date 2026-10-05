from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_tenant
from app.api.schemas import GenerateRequest, GenerateResponse
from app.db.models import Tenant
from app.db.session import get_db
from app.services.meter import record

router = APIRouter(prefix="/v1", tags=["generate"])


@router.post(
    "/generate",
    response_model=GenerateResponse,
    responses={
        200: {"description": "Successful generation metering"},
        400: {"description": "Bad Request"},
        401: {"description": "Unauthorized"},
        402: {"description": "Payment Required"},
        409: {"description": "Conflict - Request in progress"},
        422: {"description": "Unprocessable Entity"},
        429: {"description": "Quota Exceeded"},
    },
)
def generate(
    request: GenerateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    tenant: Tenant = Depends(get_current_tenant),
    db: Session = Depends(get_db),
) -> JSONResponse:
    if idempotency_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Idempotency-Key header",
        )

    key = idempotency_key.strip()
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header cannot be empty",
        )

    if len(idempotency_key) > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header exceeds maximum length of 255 characters",
        )

    body_dict = request.model_dump()
    canonical_json = json.dumps(body_dict, sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    result = record(
        session=db,
        tenant_id=tenant.id,
        key=idempotency_key,
        endpoint="/v1/generate",
        request_hash=request_hash,
        request_body=body_dict,
    )

    return JSONResponse(
        status_code=result.status_code,
        content=result.body,
        headers=result.headers,
    )
