"""Integration tests for store credit — admin per-customer endpoints
(/api/v1/admin/customers/{id}/store-credit[/adjust]) and the owner's view
(/api/v1/account/store-credit).

Covers authz (admin gating on the admin pair; 401 anonymous on the account
view), the lazy account create + signed adjust with the 400
``store_credit_balance_negative`` machine code, per-currency accounts,
newest-first ledger, owner-scoping (a customer sees only their own credit),
the audit event on adjust, and the always-on read (the ``store_credit_enabled``
toggle gates redemption UX, not visibility — reads work while it's off).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.crud import store_config as crud_store_config
from app.models.admin_audit_event import AdminAuditEvent
from app.models.user import UserRole

pytestmark = pytest.mark.integration

ACCOUNT_URL = "/api/v1/account/store-credit"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _admin_url(customer_id: int, suffix: str = "") -> str:
    return f"/api/v1/admin/customers/{customer_id}/store-credit{suffix}"


def _audit_count(db: Session) -> int:
    return db.query(AdminAuditEvent).filter(AdminAuditEvent.resource_type == "store_credit").count()


# ---------------------------------------------------------------------------
# admin: authz
# ---------------------------------------------------------------------------


async def test_admin_get_requires_admin(client, user_factory):
    customer = user_factory(email="sc-cust1@example.com")
    assert (await client.get(_admin_url(customer.id))).status_code == 401
    assert (
        await client.get(_admin_url(customer.id), headers=_headers(customer))
    ).status_code == 403


async def test_admin_adjust_requires_admin(client, user_factory):
    customer = user_factory(email="sc-cust2@example.com")
    body = {"amount": 10.0, "currency": "PLN"}
    url = _admin_url(customer.id, "/adjust")
    assert (await client.post(url, json=body)).status_code == 401
    assert (await client.post(url, json=body, headers=_headers(customer))).status_code == 403


async def test_admin_endpoints_404_unknown_customer(client, user_factory):
    admin = user_factory(email="sc-admin404@example.com", role=UserRole.ADMIN)
    assert (await client.get(_admin_url(999999), headers=_headers(admin))).status_code == 404
    resp = await client.post(
        _admin_url(999999, "/adjust"),
        json={"amount": 10.0, "currency": "PLN"},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# admin: adjust + summary
# ---------------------------------------------------------------------------


async def test_admin_adjust_creates_account_and_returns_summary(client, user_factory, db_session):
    admin = user_factory(email="sc-admin1@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="sc-cust3@example.com")
    before = _audit_count(db_session)

    resp = await client.post(
        _admin_url(customer.id, "/adjust"),
        json={"amount": 50.0, "currency": "PLN", "note": "goodwill"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accounts"] == [{"currency": "PLN", "balance": 50.0}]
    assert len(body["entries"]) == 1
    entry = body["entries"][0]
    assert entry["kind"] == "adjust"
    assert entry["amount"] == 50.0
    assert entry["balance_after"] == 50.0
    assert entry["currency"] == "PLN"
    assert entry["note"] == "goodwill"
    assert _audit_count(db_session) == before + 1


async def test_admin_adjust_accumulates_and_supports_negative(client, user_factory):
    admin = user_factory(email="sc-admin2@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="sc-cust4@example.com")
    url = _admin_url(customer.id, "/adjust")
    await client.post(url, json={"amount": 100.0, "currency": "USD"}, headers=_headers(admin))
    resp = await client.post(
        url, json={"amount": -40.0, "currency": "USD"}, headers=_headers(admin)
    )
    body = resp.json()
    assert body["accounts"] == [{"currency": "USD", "balance": 60.0}]
    # Ledger newest first.
    assert [e["amount"] for e in body["entries"]] == [-40.0, 100.0]


async def test_admin_adjust_below_zero_400_machine_code(client, user_factory):
    admin = user_factory(email="sc-admin3@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="sc-cust5@example.com")
    resp = await client.post(
        _admin_url(customer.id, "/adjust"),
        json={"amount": -5.0, "currency": "PLN"},
        headers=_headers(admin),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "store_credit_balance_negative"


async def test_admin_adjust_zero_rejected(client, user_factory):
    admin = user_factory(email="sc-admin4@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="sc-cust6@example.com")
    resp = await client.post(
        _admin_url(customer.id, "/adjust"),
        json={"amount": 0, "currency": "PLN"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_admin_get_multi_currency_summary(client, user_factory):
    admin = user_factory(email="sc-admin5@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="sc-cust7@example.com")
    url = _admin_url(customer.id, "/adjust")
    await client.post(url, json={"amount": 10.0, "currency": "PLN"}, headers=_headers(admin))
    await client.post(url, json={"amount": 20.0, "currency": "EUR"}, headers=_headers(admin))

    resp = await client.get(_admin_url(customer.id), headers=_headers(admin))
    body = resp.json()
    assert body["accounts"] == [
        {"currency": "EUR", "balance": 20.0},
        {"currency": "PLN", "balance": 10.0},
    ]
    assert len(body["entries"]) == 2


# ---------------------------------------------------------------------------
# account view (owner)
# ---------------------------------------------------------------------------


async def test_account_view_requires_auth(client):
    assert (await client.get(ACCOUNT_URL)).status_code == 401


async def test_account_view_empty_for_fresh_user(client, user_factory):
    customer = user_factory(email="sc-fresh@example.com")
    resp = await client.get(ACCOUNT_URL, headers=_headers(customer))
    assert resp.status_code == 200
    assert resp.json() == {"accounts": [], "entries": []}


async def test_account_view_shows_own_credit_only(client, user_factory):
    admin = user_factory(email="sc-admin6@example.com", role=UserRole.ADMIN)
    mine = user_factory(email="sc-mine@example.com")
    other = user_factory(email="sc-other@example.com")
    await client.post(
        _admin_url(mine.id, "/adjust"),
        json={"amount": 30.0, "currency": "PLN"},
        headers=_headers(admin),
    )
    await client.post(
        _admin_url(other.id, "/adjust"),
        json={"amount": 99.0, "currency": "PLN"},
        headers=_headers(admin),
    )

    body = (await client.get(ACCOUNT_URL, headers=_headers(mine))).json()
    assert body["accounts"] == [{"currency": "PLN", "balance": 30.0}]
    assert [e["amount"] for e in body["entries"]] == [30.0]


async def test_account_view_available_with_toggle_off(client, user_factory, db_session):
    """Reads stay available regardless of the redemption toggle (design pin)."""
    config = crud_store_config.get_singleton(db_session)
    assert config.store_credit_enabled is False  # ships dark
    admin = user_factory(email="sc-admin7@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="sc-toggled@example.com")
    await client.post(
        _admin_url(customer.id, "/adjust"),
        json={"amount": 12.5, "currency": "EUR"},
        headers=_headers(admin),
    )
    resp = await client.get(ACCOUNT_URL, headers=_headers(customer))
    assert resp.status_code == 200
    assert resp.json()["accounts"] == [{"currency": "EUR", "balance": 12.5}]
