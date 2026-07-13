"""Integration tests for the admin store-settings API (/api/v1/admin/settings).

Covers authz (401/403 on GET + PATCH), GET returns the migration-seeded
singleton, PATCH updates fields + a re-GET reflects them, partial PATCH leaves
unsent fields untouched, ``invoice_vat_rate`` range validation (422 below 0 /
above 100), and the audit row written on PATCH (resource_type ``store_config``).
The config is a single row (id=1) — there is no create/list/delete.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.user import UserRole

pytestmark = pytest.mark.integration

SETTINGS_URL = "/api/v1/admin/settings"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _audit_count(db: Session, action: str) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(
            AdminAuditEvent.resource_type == "store_config",
            AdminAuditEvent.action == action,
        )
        .count()
    )


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_get_requires_admin(client, user_factory):
    customer = user_factory(email="set-cust1@example.com", role=UserRole.CUSTOMER)
    assert (await client.get(SETTINGS_URL)).status_code == 401
    assert (await client.get(SETTINGS_URL, headers=_headers(customer))).status_code == 403


async def test_patch_requires_admin(client, user_factory):
    customer = user_factory(email="set-cust2@example.com", role=UserRole.CUSTOMER)
    body = {"store_name": "Hacked"}
    assert (await client.patch(SETTINGS_URL, json=body)).status_code == 401
    assert (
        await client.patch(SETTINGS_URL, json=body, headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def test_get_returns_seeded_singleton(client, user_factory):
    admin = user_factory(email="set-admin1@example.com", role=UserRole.ADMIN)
    resp = await client.get(SETTINGS_URL, headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 1
    # Seeded from config.py defaults.
    assert body["store_name"] == "E-commerce Store"
    assert body["invoice_vat_rate"] == 23
    # Returns window seeded by the RMA migration's server_default.
    assert body["returns_window_days"] == 30
    # Response shape includes the full contract C-fe builds against.
    for key in (
        "address_line",
        "postal_city",
        "country",
        "tax_id",
        "email",
        "updated_at",
    ):
        assert key in body


# ---------------------------------------------------------------------------
# patch
# ---------------------------------------------------------------------------


async def test_patch_updates_and_reget_reflects(client, user_factory):
    admin = user_factory(email="set-admin2@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        SETTINGS_URL,
        json={
            "store_name": "Acme Sp. z o.o.",
            "address_line": "ul. Testowa 1",
            "postal_city": "00-001 Warszawa",
            "country": "Poland",
            "tax_id": "PL1234567890",
            "email": "store@example.com",
            "invoice_vat_rate": 8,
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["store_name"] == "Acme Sp. z o.o."
    assert body["invoice_vat_rate"] == 8

    # Re-GET reflects the persisted change.
    reget = await client.get(SETTINGS_URL, headers=_headers(admin))
    assert reget.status_code == 200
    rebody = reget.json()
    assert rebody["store_name"] == "Acme Sp. z o.o."
    assert rebody["address_line"] == "ul. Testowa 1"
    assert rebody["postal_city"] == "00-001 Warszawa"
    assert rebody["country"] == "Poland"
    assert rebody["tax_id"] == "PL1234567890"
    assert rebody["email"] == "store@example.com"
    assert rebody["invoice_vat_rate"] == 8


async def test_patch_partial_leaves_other_fields_untouched(client, user_factory):
    admin = user_factory(email="set-admin3@example.com", role=UserRole.ADMIN)
    # Seed a known baseline first.
    await client.patch(
        SETTINGS_URL,
        json={"store_name": "Baseline Co.", "tax_id": "PL0000000000"},
        headers=_headers(admin),
    )
    # Now change only the VAT rate.
    resp = await client.patch(SETTINGS_URL, json={"invoice_vat_rate": 0}, headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["invoice_vat_rate"] == 0
    # Untouched fields retained.
    assert body["store_name"] == "Baseline Co."
    assert body["tax_id"] == "PL0000000000"


async def test_patch_writes_audit(client, user_factory, db_session):
    admin = user_factory(email="set-admin4@example.com", role=UserRole.ADMIN)
    before = _audit_count(db_session, "update")
    resp = await client.patch(
        SETTINGS_URL, json={"store_name": "Audited Co."}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    assert _audit_count(db_session, "update") == before + 1


@pytest.mark.parametrize("bad_rate", [-1, 101, 200])
async def test_patch_rejects_out_of_range_vat(client, user_factory, bad_rate):
    admin = user_factory(email=f"set-admin-vat{bad_rate}@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        SETTINGS_URL, json={"invoice_vat_rate": bad_rate}, headers=_headers(admin)
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("ok_rate", [0, 23, 100])
async def test_patch_accepts_boundary_vat(client, user_factory, ok_rate):
    admin = user_factory(email=f"set-admin-ok{ok_rate}@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        SETTINGS_URL, json={"invoice_vat_rate": ok_rate}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    assert resp.json()["invoice_vat_rate"] == ok_rate


@pytest.mark.parametrize("bad_color", ["red", "#12345", "#1234567", "12ab34", "#12gh56"])
async def test_patch_rejects_malformed_primary_color(client, user_factory, bad_color):
    admin = user_factory(email=f"set-admin-col-{hash(bad_color)}@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        SETTINGS_URL, json={"primary_color": bad_color}, headers=_headers(admin)
    )
    assert resp.status_code == 422


async def test_patch_primary_color_normalizes_and_clears(client, user_factory):
    admin = user_factory(email="set-admin-color@example.com", role=UserRole.ADMIN)
    # Uppercase hex is accepted and normalized to lowercase.
    resp = await client.patch(
        SETTINGS_URL, json={"primary_color": "#1E40AF"}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    assert resp.json()["primary_color"] == "#1e40af"
    # Blank clears back to the default theme.
    resp = await client.patch(SETTINGS_URL, json={"primary_color": ""}, headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json()["primary_color"] == ""


# ---------------------------------------------------------------------------
# returns_window_days (RMA)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ok_days", [0, 14, 365])
async def test_patch_updates_returns_window(client, user_factory, ok_days):
    admin = user_factory(email=f"set-admin-ret{ok_days}@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        SETTINGS_URL, json={"returns_window_days": ok_days}, headers=_headers(admin)
    )
    assert resp.status_code == 200
    assert resp.json()["returns_window_days"] == ok_days

    reget = await client.get(SETTINGS_URL, headers=_headers(admin))
    assert reget.json()["returns_window_days"] == ok_days


async def test_patch_rejects_negative_returns_window(client, user_factory):
    admin = user_factory(email="set-admin-retneg@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        SETTINGS_URL, json={"returns_window_days": -1}, headers=_headers(admin)
    )
    assert resp.status_code == 422
