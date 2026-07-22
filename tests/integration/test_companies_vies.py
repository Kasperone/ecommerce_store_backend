"""Company-portal VIES-check + VAT-ID lifecycle.

- POST /api/v1/companies/me/vies-check — a COMPANY_ADMIN validates its own
  company's VAT-ID (self-service onboarding, off the checkout path).
- Changing the VAT-ID (admin or portal PATCH) resets any prior confirmation, so
  a company can't carry a stale "valid" from a different number into U4.
"""

import httpx
import pytest
import respx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token
from app.models.company import Company
from app.models.company_user import CompanyRole
from app.models.user import UserRole
from tests.factories.company import CompanyFactory
from tests.factories.company_user import CompanyUserFactory

pytestmark = pytest.mark.integration

VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"
PORTAL_CHECK = "/api/v1/companies/me/vies-check"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def company_factory(db_session: Session):
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs) -> Company:
        return CompanyFactory(**kwargs)

    return _make


@pytest.fixture
def company_user_factory(db_session: Session):
    CompanyUserFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyUserFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyUserFactory(**kwargs)

    return _make


def _admin_account(user_factory, company_factory, company_user_factory, email, **company_kw):
    user = user_factory(email=email, role=UserRole.CUSTOMER)
    company = company_factory(**company_kw)
    company_user_factory(
        company_id=company.id, user_id=user.id, role=CompanyRole.COMPANY_ADMIN.value
    )
    return user, company


async def test_portal_check_requires_admin_member(client, user_factory):
    user = user_factory(email="vies-portal-none@example.com", role=UserRole.CUSTOMER)
    resp = await client.post(PORTAL_CHECK, headers=_headers(user))
    assert resp.status_code == 404  # not a member of any company


@respx.mock
async def test_portal_admin_validates_own_company(
    client, user_factory, company_factory, company_user_factory, monkeypatch
):
    monkeypatch.setattr(settings, "VIES_ENABLED", True)
    respx.post(VIES_URL).mock(
        return_value=httpx.Response(200, json={"valid": True, "name": "SELF SP Z OO"})
    )
    user, _ = _admin_account(
        user_factory,
        company_factory,
        company_user_factory,
        "vies-portal-admin@example.com",
        name="Self Co",
        vat_id="PL5252445724",
    )
    resp = await client.post(PORTAL_CHECK, headers=_headers(user))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vies_valid"] is True
    assert body["vies_confirmed_name"] == "SELF SP Z OO"
    assert body["role"] == CompanyRole.COMPANY_ADMIN.value


async def test_portal_503_when_disabled(
    client, user_factory, company_factory, company_user_factory, monkeypatch
):
    monkeypatch.setattr(settings, "VIES_ENABLED", False)
    user, _ = _admin_account(
        user_factory,
        company_factory,
        company_user_factory,
        "vies-portal-off@example.com",
        name="Off Co",
        vat_id="PL5252445724",
    )
    resp = await client.post(PORTAL_CHECK, headers=_headers(user))
    assert resp.status_code == 503, resp.text


async def test_changing_vat_id_resets_confirmation(
    client, user_factory, company_factory, db_session
):
    """A confirmed company that changes its VAT-ID must lose the confirmation —
    the stored ``vies_valid`` pertained to the old number."""
    from datetime import datetime

    admin = user_factory(email="vies-reset@example.com", role=UserRole.ADMIN)
    company = company_factory(
        name="Reset Co",
        vat_id="PL5252445724",
        vies_valid=True,
        vies_confirmed_at=datetime(2026, 6, 1),
        vies_confirmed_name="OLD NAME",
    )
    resp = await client.patch(
        f"/api/v1/admin/companies/{company.id}",
        json={"vat_id": "DE811907980"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vat_id"] == "DE811907980"
    assert body["vies_valid"] is False
    assert body["vies_confirmed_at"] is None
    assert body["vies_confirmed_name"] is None
