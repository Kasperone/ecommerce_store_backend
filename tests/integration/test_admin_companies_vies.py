"""Admin VIES-check endpoint — POST /api/v1/admin/companies/{id}/vies-check.

Covers the kill-switch (503 when disabled), the no-VAT-ID guard (422), a valid
confirmation (respx-mocked VIES), and a fail-closed outage (502 with the attempt
still recorded). VIES itself is mocked — no real EU call from CI.
"""

import httpx
import pytest
import respx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token
from app.models.company import Company
from app.models.user import UserRole
from tests.factories.company import CompanyFactory

pytestmark = pytest.mark.integration

VIES_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"


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


def _url(company_id: int) -> str:
    return f"/api/v1/admin/companies/{company_id}/vies-check"


async def test_requires_auth(client, company_factory):
    company = company_factory(name="NoAuth", vat_id="PL5252445724")
    resp = await client.post(_url(company.id))
    assert resp.status_code == 401


async def test_503_when_disabled(client, user_factory, company_factory, monkeypatch):
    monkeypatch.setattr(settings, "VIES_ENABLED", False)
    admin = user_factory(email="vies-a@example.com", role=UserRole.ADMIN)
    company = company_factory(name="Disabled", vat_id="PL5252445724")
    resp = await client.post(_url(company.id), headers=_headers(admin))
    assert resp.status_code == 503, resp.text


async def test_422_when_no_vat_id(client, user_factory, company_factory, monkeypatch):
    monkeypatch.setattr(settings, "VIES_ENABLED", True)
    admin = user_factory(email="vies-b@example.com", role=UserRole.ADMIN)
    company = company_factory(name="No VAT")
    resp = await client.post(_url(company.id), headers=_headers(admin))
    assert resp.status_code == 422, resp.text


async def test_404_for_unknown_company(client, user_factory, monkeypatch):
    monkeypatch.setattr(settings, "VIES_ENABLED", True)
    admin = user_factory(email="vies-c@example.com", role=UserRole.ADMIN)
    resp = await client.post(_url(999999), headers=_headers(admin))
    assert resp.status_code == 404, resp.text


@respx.mock
async def test_valid_confirms_and_returns_status(
    client, user_factory, company_factory, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "VIES_ENABLED", True)
    respx.post(VIES_URL).mock(
        return_value=httpx.Response(
            200, json={"valid": True, "name": "ACME SP Z OO", "requestIdentifier": "REQ-7"}
        )
    )
    admin = user_factory(email="vies-d@example.com", role=UserRole.ADMIN)
    company = company_factory(name="Acme", vat_id="PL5252445724")
    resp = await client.post(_url(company.id), headers=_headers(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vies_valid"] is True
    assert body["vies_confirmed_name"] == "ACME SP Z OO"
    assert body["vies_confirmed_at"] is not None
    # Proof-of-check is stored but never on the wire.
    assert "vies_request_id" not in body


@respx.mock
async def test_outage_returns_502_but_records_attempt(
    client, user_factory, company_factory, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "VIES_ENABLED", True)
    respx.post(VIES_URL).mock(
        return_value=httpx.Response(200, json={"valid": False, "userError": "MS_UNAVAILABLE"})
    )
    admin = user_factory(email="vies-e@example.com", role=UserRole.ADMIN)
    company = company_factory(name="Down", vat_id="PL5252445724")
    resp = await client.post(_url(company.id), headers=_headers(admin))
    assert resp.status_code == 502, resp.text
    db_session.refresh(company)
    assert company.vies_last_checked_at is not None
    assert company.vies_valid is False
