"""Idempotent seed: one Free tenant and one Pro tenant.

Dedup strategy
--------------
Each seed tenant has a *fixed*, deterministic seed phrase baked into this file.
The plaintext key is derived from that phrase via SHA-256 (hex) for display
and then hashed a second time (SHA-256 of the hex string) for storage in
api_key_hash.

The INSERT uses ON CONFLICT (api_key_hash) DO NOTHING, which relies on the
real UNIQUE constraint that already exists on tenants(api_key_hash).  Because
the hash is deterministic, re-running the script produces the same INSERT that
silently no-ops on conflict — no application-level read-before-write, no race
window, no extra rows, ever.

Plaintext keys are printed only when a tenant is actually created in this run
(INSERT rowcount == 1).  On subsequent runs, tenant info is printed without
re-printing secrets.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

# Allow `python scripts/seed.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text

from app.db.models import Plan, PlanCode, Tenant
from app.db.session import SessionLocal

# ── Fixed seed phrases ────────────────────────────────────────────────────────
# These never change after the first deployment.  Rotating them would create
# a new api_key_hash and therefore a new tenant row — don't do that.
_SEED_FREE_PHRASE = "seed-phrase:free-tenant:v1"
_SEED_PRO_PHRASE  = "seed-phrase:pro-tenant:v1"

SEED_FREE_NAME = "seed-free"
SEED_PRO_NAME  = "seed-pro"


def _derive_key(phrase: str) -> str:
    """Deterministic 'API key' displayed to the operator on first creation."""
    return "sk_seed_" + hashlib.sha256(phrase.encode()).hexdigest()


def _hash_key(plaintext: str) -> str:
    """Second-pass hash stored in api_key_hash — never the plaintext."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def _upsert_tenant(
    session,
    *,
    name: str,
    plan: Plan,
    phrase: str,
) -> tuple[Tenant, str | None]:
    """
    Insert the seed tenant via ON CONFLICT (api_key_hash) DO NOTHING.

    Returns (tenant_obj, plaintext_key_if_just_created).
    The plaintext is only returned when rowcount == 1 (i.e., we just inserted).
    """
    plaintext   = _derive_key(phrase)
    stored_hash = _hash_key(plaintext)

    # Single atomic statement — relies on the UNIQUE constraint, not app logic.
    result = session.execute(
        text(
            """
            INSERT INTO tenants (name, plan_id, api_key_hash)
            VALUES (:name, :plan_id, :hash)
            ON CONFLICT (api_key_hash) DO NOTHING
            """
        ),
        {"name": name, "plan_id": plan.id, "hash": stored_hash},
    )
    just_created = result.rowcount == 1

    tenant = session.scalar(
        select(Tenant).where(Tenant.api_key_hash == stored_hash)
    )
    return tenant, (plaintext if just_created else None)


def main() -> None:
    session = SessionLocal()
    try:
        free_plan = session.scalar(select(Plan).where(Plan.code == PlanCode.free))
        pro_plan  = session.scalar(select(Plan).where(Plan.code == PlanCode.pro))
        if free_plan is None or pro_plan is None:
            raise SystemExit(
                "plans table is empty; run `alembic upgrade head` before seeding"
            )

        free_tenant, free_key = _upsert_tenant(
            session, name=SEED_FREE_NAME, plan=free_plan, phrase=_SEED_FREE_PHRASE
        )
        pro_tenant, pro_key = _upsert_tenant(
            session, name=SEED_PRO_NAME, plan=pro_plan, phrase=_SEED_PRO_PHRASE
        )
        session.commit()

        _print_tenant(free_tenant, free_plan, free_key)
        _print_tenant(pro_tenant,  pro_plan,  pro_key)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _print_tenant(tenant: Tenant, plan: Plan, plaintext_key: str | None) -> None:
    print(
        f"tenant name={tenant.name} id={tenant.id} plan={plan.code.value} "
        f"plan_id={tenant.plan_id}"
    )
    if plaintext_key is not None:
        print(f"API key (shown once): {plaintext_key}")
    else:
        print("API key: already created; plaintext not re-printed")


if __name__ == "__main__":
    main()
