from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import IdemState, IdempotencyKey


@dataclass
class Claim:
    status: str
    id: int | None = None
    response_status: int | None = None
    response_body: Any = None


def claim(
    session: Session,
    tenant_id: uuid.UUID | str,
    key: str,
    endpoint: str,
    request_hash: str,
) -> Claim:
    if isinstance(tenant_id, str):
        tenant_id = uuid.UUID(tenant_id)

    stmt = (
        insert(IdempotencyKey)
        .values(
            tenant_id=tenant_id,
            key=key,
            endpoint=endpoint,
            request_hash=request_hash,
            state=IdemState.in_progress,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "key"])
        .returning(IdempotencyKey.id)
    )

    result = session.execute(stmt)
    new_id = result.scalar_one_or_none()

    if new_id is not None:
        return Claim(status="OWNED", id=new_id)

    # 0 rows inserted -> conflict occurred; select existing record
    existing = session.execute(
        sa.select(
            IdempotencyKey.id,
            IdempotencyKey.state,
            IdempotencyKey.request_hash,
            IdempotencyKey.response_status,
            IdempotencyKey.response_body,
        ).where(
            IdempotencyKey.tenant_id == tenant_id,
            IdempotencyKey.key == key,
        )
    ).mappings().one_or_none()

    if existing is not None:
        state_val = (
            existing["state"].value
            if isinstance(existing["state"], IdemState)
            else str(existing["state"])
        )
        if state_val == IdemState.completed.value or state_val == "completed":
            if existing["request_hash"] == request_hash:
                return Claim(
                    status="REPLAY",
                    id=existing["id"],
                    response_status=existing["response_status"],
                    response_body=existing["response_body"],
                )
            return Claim(status="HASH_MISMATCH", id=existing["id"])
        elif state_val == IdemState.in_progress.value or state_val == "in_progress":
            return Claim(status="IN_PROGRESS", id=existing["id"])

    return Claim(status="IN_PROGRESS")


def complete(
    session: Session,
    claim_id: int,
    response_status: int,
    response_body: Any,
) -> None:
    stmt = (
        sa.update(IdempotencyKey)
        .where(IdempotencyKey.id == claim_id)
        .values(
            state=IdemState.completed,
            response_status=response_status,
            response_body=response_body,
            completed_at=sa.func.now(),
        )
    )
    session.execute(stmt)
