import concurrent.futures
import hashlib
import threading
import uuid

import pytest
import sqlalchemy as sa

from app.db.models import IdempotencyKey, Plan, PlanCode, Tenant
from app.db.session import SessionLocal
from app.repos.idempotency import Claim, claim, complete


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def test_tenant(db_session):
    plan = db_session.scalar(sa.select(Plan).where(Plan.code == PlanCode.free))
    if not plan:
        plan = Plan(
            code=PlanCode.free,
            display_name="Free",
            api_call_limit=1000,
            token_limit=100000,
        )
        db_session.add(plan)
        db_session.commit()

    tenant = Tenant(
        name=f"tenant-idem-{uuid.uuid4().hex[:8]}",
        plan_id=plan.id,
        api_key_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
    )
    db_session.add(tenant)
    db_session.commit()
    return tenant


def test_first_claim_fresh_key(db_session, test_tenant):
    key = f"key-{uuid.uuid4().hex}"
    request_hash = "hash_fresh_123"

    res = claim(
        session=db_session,
        tenant_id=test_tenant.id,
        key=key,
        endpoint="/v1/generate",
        request_hash=request_hash,
    )
    db_session.commit()

    assert isinstance(res, Claim)
    assert res.status == "OWNED"
    assert res.id is not None

    # Verify state in DB
    row = db_session.scalar(
        sa.select(IdempotencyKey).where(IdempotencyKey.id == res.id)
    )
    assert row is not None
    assert row.key == key
    assert row.request_hash == request_hash
    assert row.state.value == "in_progress" or row.state == "in_progress"


def test_claim_replay_after_complete(db_session, test_tenant):
    key = f"key-{uuid.uuid4().hex}"
    request_hash = "hash_replay_123"
    response_body = {"result": "success", "cost_micro_cents": 300}
    response_status = 200

    # 1. First claim
    res1 = claim(
        session=db_session,
        tenant_id=test_tenant.id,
        key=key,
        endpoint="/v1/generate",
        request_hash=request_hash,
    )
    assert res1.status == "OWNED"
    assert res1.id is not None

    # Complete the claim
    complete(
        session=db_session,
        claim_id=res1.id,
        response_status=response_status,
        response_body=response_body,
    )
    db_session.commit()

    # 2. Second claim with same tenant, key, and hash in a separate session
    session2 = SessionLocal()
    try:
        res2 = claim(
            session=session2,
            tenant_id=test_tenant.id,
            key=key,
            endpoint="/v1/generate",
            request_hash=request_hash,
        )
        assert res2.status == "REPLAY"
        assert res2.response_status == 200
        assert res2.response_body == response_body
    finally:
        session2.close()


def test_claim_hash_mismatch(db_session, test_tenant):
    key = f"key-{uuid.uuid4().hex}"
    hash_original = "hash_original_456"
    hash_modified = "hash_modified_789"

    # 1. First claim and complete
    res1 = claim(
        session=db_session,
        tenant_id=test_tenant.id,
        key=key,
        endpoint="/v1/generate",
        request_hash=hash_original,
    )
    assert res1.status == "OWNED"
    complete(
        session=db_session,
        claim_id=res1.id,
        response_status=200,
        response_body={"ok": True},
    )
    db_session.commit()

    # 2. Claim again with same key but different hash
    session2 = SessionLocal()
    try:
        res2 = claim(
            session=session2,
            tenant_id=test_tenant.id,
            key=key,
            endpoint="/v1/generate",
            request_hash=hash_modified,
        )
        assert res2.status == "HASH_MISMATCH"
    finally:
        session2.close()


def test_claim_in_progress(db_session, test_tenant):
    key = f"key-{uuid.uuid4().hex}"
    request_hash = "hash_inflight_999"

    # First claim starts and is committed to DB in state='in_progress', but never completed
    res1 = claim(
        session=db_session,
        tenant_id=test_tenant.id,
        key=key,
        endpoint="/v1/generate",
        request_hash=request_hash,
    )
    assert res1.status == "OWNED"
    db_session.commit()

    # Second claim while still in_progress
    session2 = SessionLocal()
    try:
        res2 = claim(
            session=session2,
            tenant_id=test_tenant.id,
            key=key,
            endpoint="/v1/generate",
            request_hash=request_hash,
        )
        assert res2.status == "IN_PROGRESS"
    finally:
        session2.close()


def test_concurrency_claims(test_tenant):
    num_workers = 12  # At least 10 workers
    key = f"key-concurrent-{uuid.uuid4().hex}"
    request_hash = "hash_concurrent_test"
    endpoint = "/v1/generate"
    tenant_id = test_tenant.id

    barrier = threading.Barrier(num_workers)

    def worker_action():
        # Each worker opens its own separate DB session/connection
        worker_session = SessionLocal()
        try:
            # Wait for all threads to be ready so they fire simultaneously
            barrier.wait()
            claim_result = claim(
                session=worker_session,
                tenant_id=tenant_id,
                key=key,
                endpoint=endpoint,
                request_hash=request_hash,
            )
            worker_session.commit()
            return claim_result
        except Exception:
            worker_session.rollback()
            raise
        finally:
            worker_session.close()

    # Execute concurrently across real threads with separate DB connections
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(worker_action) for _ in range(num_workers)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    assert len(results) == num_workers
    statuses = [r.status for r in results]

    # Exactly one worker wins the claim and gets OWNED
    owned_count = statuses.count("OWNED")
    in_progress_count = statuses.count("IN_PROGRESS")

    assert owned_count == 1, f"Expected exactly 1 OWNED, got {owned_count}. Statuses: {statuses}"
    assert in_progress_count == num_workers - 1, (
        f"Expected {num_workers - 1} IN_PROGRESS, got {in_progress_count}. Statuses: {statuses}"
    )
