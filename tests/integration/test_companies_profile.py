"""Integration tests for ``PATCH /companies/me`` — company-profile self-service
(Wave D / B2B-2 slice 4, D-D-4).

Covers:

- A COMPANY_ADMIN updates the profile fields (regon / billing_* / default_currency)
  → 200 + the new values surface on the response (and on a subsequent GET).
- A BUYER / VIEWER / APPROVER member → 403 (member but not admin).
- A non-member → 404 (anti-enumeration: the route doesn't exist for them).
- Auth required → 401.
- Partial update only touches the sent fields (exclude_unset).
- An empty-string clears a field; ``default_currency`` is upper-cased.
- The allow-list holds: ``name`` / ``status`` / ``primary_user_id`` sent in the
  body are ignored (extra fields, not model fields on the schema).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.company_user import CompanyRole
from app.models.user import UserRole
from tests.factories.company import CompanyFactory
from tests.factories.company_user import CompanyUserFactory

pytestmark = pytest.mark.integration

PROFILE_PATH = "/api/v1/companies/me"


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


def _admin_account(user_factory, company_factory, company_user_factory, email: str):
    """A user + company + an active COMPANY_ADMIN membership linking them."""
    user = user_factory(email=email, role=UserRole.CUSTOMER)
    company = company_factory(name="Acme Co")
    company_user_factory(
        company_id=company.id, user_id=user.id, role=CompanyRole.COMPANY_ADMIN.value
    )
    return user, company


# ---------------------------------------------------------------------------
# Auth + role gating
# ---------------------------------------------------------------------------


async def test_patch_requires_auth(client):
    resp = await client.patch(PROFILE_PATH, json={"regon": "123456785"})
    assert resp.status_code == 401


async def test_patch_404_for_non_member(client, user_factory):
    user = user_factory(email="profile-none@example.com", role=UserRole.CUSTOMER)
    resp = await client.patch(PROFILE_PATH, json={"regon": "1"}, headers=_headers(user))
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "role", [CompanyRole.BUYER.value, CompanyRole.VIEWER.value, CompanyRole.APPROVER.value]
)
async def test_patch_403_for_non_admin_member(
    client, user_factory, company_factory, company_user_factory, role
):
    user = user_factory(email=f"profile-{role}@example.com", role=UserRole.CUSTOMER)
    company = company_factory(name=f"{role} Co")
    company_user_factory(company_id=company.id, user_id=user.id, role=role)

    resp = await client.patch(PROFILE_PATH, json={"regon": "1"}, headers=_headers(user))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_company_admin_updates_profile(
    client, user_factory, company_factory, company_user_factory
):
    user, company = _admin_account(
        user_factory, company_factory, company_user_factory, "profile-admin@example.com"
    )
    body = {
        "tax_id": "PL1234567890",
        "regon": "123456785",
        "email": "billing@acme.example",
        "phone": "+48123456789",
        "billing_address_street": "ul. Testowa 1",
        "billing_address_city": "Warszawa",
        "billing_address_postal_code": "00-001",
        "billing_address_country": "PL",
        "billing_address_state": "Mazowieckie",
        "default_currency": "pln",
    }
    resp = await client.patch(PROFILE_PATH, json=body, headers=_headers(user))
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == company.id
    assert data["regon"] == "123456785"
    assert data["billing_address_city"] == "Warszawa"
    assert data["tax_id"] == "PL1234567890"
    # default_currency is normalised to upper-case.
    assert data["default_currency"] == "PLN"
    # The caller's role is echoed (same shape as GET /companies/me).
    assert data["role"] == "COMPANY_ADMIN"

    # Persisted: a subsequent GET reflects the change.
    get_resp = await client.get(PROFILE_PATH, headers=_headers(user))
    assert get_resp.status_code == 200
    assert get_resp.json()["regon"] == "123456785"
    assert get_resp.json()["default_currency"] == "PLN"


async def test_partial_update_only_touches_sent_fields(
    client, user_factory, company_factory, company_user_factory, db_session
):
    user, _ = _admin_account(
        user_factory, company_factory, company_user_factory, "profile-partial@example.com"
    )
    # Seed two fields, then PATCH only one — the other must be untouched.
    await client.patch(
        PROFILE_PATH,
        json={"regon": "111111118", "billing_address_city": "Kraków"},
        headers=_headers(user),
    )
    resp = await client.patch(
        PROFILE_PATH, json={"billing_address_city": "Gdańsk"}, headers=_headers(user)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["billing_address_city"] == "Gdańsk"
    assert data["regon"] == "111111118"  # untouched by the second PATCH


async def test_empty_string_clears_field(
    client, user_factory, company_factory, company_user_factory
):
    user, _ = _admin_account(
        user_factory, company_factory, company_user_factory, "profile-clear@example.com"
    )
    await client.patch(PROFILE_PATH, json={"regon": "222222226"}, headers=_headers(user))
    resp = await client.patch(PROFILE_PATH, json={"regon": "   "}, headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json()["regon"] is None


async def test_allowlist_ignores_admin_only_fields(
    client, user_factory, company_factory, company_user_factory
):
    """name / status / primary_user_id are not on CompanyProfileUpdate — sending
    them must NOT change the company (Pydantic drops unknown fields by default)."""
    user, _ = _admin_account(
        user_factory, company_factory, company_user_factory, "profile-allowlist@example.com"
    )
    resp = await client.patch(
        PROFILE_PATH,
        json={
            "regon": "333333334",
            "name": "HACKED",
            "status": "approved",
            "primary_user_id": 999999,
        },
        headers=_headers(user),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["regon"] == "333333334"  # the allowed field applied
    assert data["name"] == "Acme Co"  # name unchanged
    assert data["status"] == "pending"  # status unchanged (still the approval gate)


async def test_too_long_currency_rejected_422(
    client, user_factory, company_factory, company_user_factory
):
    user, _ = _admin_account(
        user_factory, company_factory, company_user_factory, "profile-422@example.com"
    )
    resp = await client.patch(
        PROFILE_PATH, json={"default_currency": "EURO"}, headers=_headers(user)
    )
    assert resp.status_code == 422
