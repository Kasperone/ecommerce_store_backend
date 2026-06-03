"""Integration tests for ``GET /companies/me`` after the Wave-D membership
generalization (B2B-2 slice 3).

Covers:

- A user with an ``active`` ``company_users`` membership gets their company +
  their stored ``role`` (role honoured — not always COMPANY_ADMIN).
- A company's denormalized primary contact (no explicit membership row) still
  resolves — as COMPANY_ADMIN (the Wave-C backward-compat path; the existing
  ``test_quotes_customer.py`` suite relies on this and must stay green).
- A non-member (neither membership nor primary contact) → 200 with a null body
  (the FE probes this per-page, so it must not 404-spam the console).
- An ``invited`` / ``suspended`` membership does NOT grant access → 200 null.
- Auth required (401).

Plus a backfill-logic test: the migration's INSERT...SELECT creates exactly one
``active`` COMPANY_ADMIN membership per company that has a non-null
``primary_user_id`` and is idempotent (the ``NOT EXISTS`` guard).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company_user import CompanyMemberStatus, CompanyRole
from app.models.user import UserRole
from tests.factories.company import CompanyFactory
from tests.factories.company_user import CompanyUserFactory

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def company_factory(db_session: Session):
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyFactory(**kwargs)

    return _make


@pytest.fixture
def company_user_factory(db_session: Session):
    CompanyUserFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyUserFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyUserFactory(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# GET /companies/me — membership resolution + role
# ---------------------------------------------------------------------------


async def test_me_requires_auth(client):
    assert (await client.get("/api/v1/companies/me")).status_code == 401


async def test_me_returns_company_and_role_for_member(
    client, user_factory, company_factory, company_user_factory
):
    user = user_factory(email="me-member@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Member Co")
    company_user_factory(company_id=company.id, user_id=user.id, role=CompanyRole.BUYER.value)

    resp = await client.get("/api/v1/companies/me", headers=_headers(user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == company.id
    assert body["name"] == "Member Co"
    assert body["status"] == "pending"
    # The caller's stored role is surfaced (BUYER, not coerced to COMPANY_ADMIN).
    assert body["role"] == "BUYER"


async def test_me_primary_contact_resolves_as_company_admin(client, user_factory, company_factory):
    """Wave-C backward-compat: a primary contact with NO membership row resolves.

    Mirrors how ``test_quotes_customer.py`` builds a B2B account — proves the
    generalization didn't break the existing quote-request flow.
    """
    user = user_factory(email="me-primary@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Primary Co", primary_user_id=user.id)

    resp = await client.get("/api/v1/companies/me", headers=_headers(user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == company.id
    assert body["role"] == "COMPANY_ADMIN"


async def test_me_returns_null_for_non_member(client, user_factory):
    """A non-member gets 200 with a null body (not 404).

    The FE probes this endpoint on every authed page to gate the B2B surfaces;
    a 404 would spam the browser console on every cold load. Returning null is
    safe here — the caller is asking about their OWN membership, not enumerating
    someone else's resource.
    """
    user = user_factory(email="me-none@example.com", role=UserRole.CUSTOMER)
    resp = await client.get("/api/v1/companies/me", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json() is None


@pytest.mark.parametrize(
    "bad_status", [CompanyMemberStatus.INVITED.value, CompanyMemberStatus.SUSPENDED.value]
)
async def test_me_returns_null_for_non_active_membership(
    client, user_factory, company_factory, company_user_factory, bad_status
):
    user = user_factory(email=f"me-{bad_status}@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name=f"{bad_status} Co")
    company_user_factory(company_id=company.id, user_id=user.id, status=bad_status)

    resp = await client.get("/api/v1/companies/me", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json() is None


async def test_me_membership_role_wins_over_primary_pointer(
    client, user_factory, company_factory, company_user_factory
):
    """An explicit membership row's role takes precedence over the pointer."""
    user = user_factory(email="me-both@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name="Both Co", primary_user_id=user.id)
    company_user_factory(company_id=company.id, user_id=user.id, role=CompanyRole.VIEWER.value)

    resp = await client.get("/api/v1/companies/me", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json()["role"] == "VIEWER"


# ---------------------------------------------------------------------------
# Migration backfill logic — one active COMPANY_ADMIN per primary_user_id
# ---------------------------------------------------------------------------

# The exact INSERT...SELECT the migration runs (kept in lockstep with
# ``alembic/versions/2b1ffb455ef9_*``). Asserting it here proves: one row per
# company with a primary contact, role COMPANY_ADMIN, status active, idempotent.
_BACKFILL_SQL = """
    INSERT INTO company_users (
        company_id, user_id, role, status, created_at, updated_at
    )
    SELECT
        c.id,
        c.primary_user_id,
        'COMPANY_ADMIN',
        'active',
        NOW(),
        NOW()
    FROM companies c
    WHERE c.primary_user_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM company_users cu
          WHERE cu.company_id = c.id AND cu.user_id = c.primary_user_id
      )
"""


def test_backfill_creates_one_admin_per_primary_contact(db_session, user_factory, company_factory):
    # Two companies WITH a primary contact, one WITHOUT.
    u1 = user_factory(email="bf1@example.com", role=UserRole.CUSTOMER)
    u2 = user_factory(email="bf2@example.com", role=UserRole.CUSTOMER)
    c1 = company_factory(name="BF One", primary_user_id=u1.id)
    c2 = company_factory(name="BF Two", primary_user_id=u2.id)
    company_factory(name="BF None")  # no primary contact → no membership

    db_session.execute(text(_BACKFILL_SQL))
    db_session.flush()

    rows = db_session.execute(
        text("SELECT company_id, user_id, role, status FROM company_users " "ORDER BY company_id")
    ).all()
    assert len(rows) == 2
    by_company = {r.company_id: r for r in rows}
    assert by_company[c1.id].user_id == u1.id
    assert by_company[c1.id].role == "COMPANY_ADMIN"
    assert by_company[c1.id].status == "active"
    assert by_company[c2.id].user_id == u2.id

    # Idempotent: re-running inserts nothing (NOT EXISTS guard).
    db_session.execute(text(_BACKFILL_SQL))
    db_session.flush()
    count = db_session.execute(text("SELECT COUNT(*) AS n FROM company_users")).scalar()
    assert count == 2
