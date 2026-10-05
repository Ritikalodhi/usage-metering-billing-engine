import pytest
import sqlalchemy as sa

from app.db.models import Tenant
from app.db.session import SessionLocal

TEST_TENANT_PREFIXES = ("test-tenant-", "tenant-idem-", "tenant-meter-", "tenant-api-")
PROTECTED_TENANT_NAMES = {"seed-free", "seed-pro"}


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_tenants():
    yield
    session = SessionLocal()
    try:
        prefix_conditions = [Tenant.name.startswith(prefix) for prefix in TEST_TENANT_PREFIXES]
        stmt = (
            sa.delete(Tenant)
            .where(sa.or_(*prefix_conditions))
            .where(Tenant.name.not_in(PROTECTED_TENANT_NAMES))
        )
        session.execute(stmt)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
