"""Integration tests for the admin shipping API.

Covers authz, zones/methods/rates CRUD, the country-conflict + rate-conflict
400 responses, audit-row recording, and the rate-by-zone/method/currency
list filters.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_zones_list_requires_admin(client, user_factory):
    customer = user_factory(email="ship-cust@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/shipping/zones")).status_code == 401
    assert (
        await client.get("/api/v1/admin/shipping/zones", headers=_headers(customer))
    ).status_code == 403


async def test_methods_list_requires_admin(client, user_factory):
    customer = user_factory(email="ship-cust2@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/shipping/methods")).status_code == 401
    assert (
        await client.get("/api/v1/admin/shipping/methods", headers=_headers(customer))
    ).status_code == 403


async def test_rates_list_requires_admin(client, user_factory):
    customer = user_factory(email="ship-cust3@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/shipping/rates")).status_code == 401
    assert (
        await client.get("/api/v1/admin/shipping/rates", headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# Zones CRUD + audit
# ---------------------------------------------------------------------------


async def test_zones_full_lifecycle_and_audit(client, user_factory, db_session):
    admin = user_factory(email="ship-admin@example.com", role=UserRole.ADMIN)

    create = await client.post(
        "/api/v1/admin/shipping/zones",
        json={"name": "Test Lifecycle Zone", "country_codes": ["NO", "DK"]},
        headers=_headers(admin),
    )
    assert create.status_code == 201, create.text
    zone_id = create.json()["id"]
    assert sorted(create.json()["country_codes"]) == ["DK", "NO"]

    # PUT replaces the country set.
    upd = await client.put(
        f"/api/v1/admin/shipping/zones/{zone_id}",
        json={"name": "Test Lifecycle Zone", "country_codes": ["NO", "SE"]},
        headers=_headers(admin),
    )
    assert upd.status_code == 200
    assert sorted(upd.json()["country_codes"]) == ["NO", "SE"]

    # GET detail reflects the latest state.
    got = await client.get(f"/api/v1/admin/shipping/zones/{zone_id}", headers=_headers(admin))
    assert sorted(got.json()["country_codes"]) == ["NO", "SE"]

    # DELETE cascades.
    deleted = await client.delete(
        f"/api/v1/admin/shipping/zones/{zone_id}", headers=_headers(admin)
    )
    assert deleted.status_code == 204
    missing = await client.get(f"/api/v1/admin/shipping/zones/{zone_id}", headers=_headers(admin))
    assert missing.status_code == 404

    # Three audit rows landed: create, update, delete (all resource_type="shipping_zone").
    rows = (
        db_session.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "shipping_zone")
        .order_by(AdminAuditEvent.id)
        .all()
    )
    actions = [r.action for r in rows]
    assert actions == ["create", "update", "delete"]


async def test_zone_create_400_on_country_already_in_other_zone(client, user_factory):
    admin = user_factory(email="ship-admin-conflict@example.com", role=UserRole.ADMIN)
    a = await client.post(
        "/api/v1/admin/shipping/zones",
        json={"name": "Conflict Zone A", "country_codes": ["KE"]},
        headers=_headers(admin),
    )
    assert a.status_code == 201
    b = await client.post(
        "/api/v1/admin/shipping/zones",
        json={"name": "Conflict Zone B", "country_codes": ["KE"]},
        headers=_headers(admin),
    )
    assert b.status_code == 400
    assert "KE" in b.json()["detail"]
    assert "Conflict Zone A" in b.json()["detail"]


async def test_zone_create_422_on_non_iso2_country(client, user_factory):
    admin = user_factory(email="ship-admin-iso@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/admin/shipping/zones",
        json={"name": "ISO Test", "country_codes": ["poland"]},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Methods CRUD + audit
# ---------------------------------------------------------------------------


async def test_methods_full_lifecycle_and_audit(client, user_factory, db_session):
    admin = user_factory(email="ship-method-admin@example.com", role=UserRole.ADMIN)

    create = await client.post(
        "/api/v1/admin/shipping/methods",
        json={"name": "Lifecycle Method", "code": "lifecycle"},
        headers=_headers(admin),
    )
    assert create.status_code == 201, create.text
    method_id = create.json()["id"]

    upd = await client.put(
        f"/api/v1/admin/shipping/methods/{method_id}",
        json={"description": "Updated"},
        headers=_headers(admin),
    )
    assert upd.status_code == 200
    assert upd.json()["description"] == "Updated"

    deleted = await client.delete(
        f"/api/v1/admin/shipping/methods/{method_id}", headers=_headers(admin)
    )
    assert deleted.status_code == 204

    rows = (
        db_session.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "shipping_method")
        .order_by(AdminAuditEvent.id)
        .all()
    )
    assert [r.action for r in rows] == ["create", "update", "delete"]


async def test_method_duplicate_code_400(client, user_factory):
    admin = user_factory(email="ship-method-dup@example.com", role=UserRole.ADMIN)
    a = await client.post(
        "/api/v1/admin/shipping/methods",
        json={"name": "Dup1", "code": "dup_code"},
        headers=_headers(admin),
    )
    assert a.status_code == 201
    b = await client.post(
        "/api/v1/admin/shipping/methods",
        json={"name": "Dup2", "code": "dup_code"},
        headers=_headers(admin),
    )
    assert b.status_code == 400


# ---------------------------------------------------------------------------
# Rates CRUD + audit
# ---------------------------------------------------------------------------


async def _seed_zone_and_method(client, admin, *, zone_name: str, country: str, method_code: str):
    z = await client.post(
        "/api/v1/admin/shipping/zones",
        json={"name": zone_name, "country_codes": [country]},
        headers=_headers(admin),
    )
    m = await client.post(
        "/api/v1/admin/shipping/methods",
        json={"name": f"M-{method_code}", "code": method_code},
        headers=_headers(admin),
    )
    return z.json()["id"], m.json()["id"]


async def test_rates_full_lifecycle_and_audit(client, user_factory, db_session):
    admin = user_factory(email="ship-rate-admin@example.com", role=UserRole.ADMIN)
    z, m = await _seed_zone_and_method(
        client, admin, zone_name="Rate Zone", country="AT", method_code="rate-method"
    )

    create = await client.post(
        "/api/v1/admin/shipping/rates",
        json={"zone_id": z, "method_id": m, "currency": "EUR", "amount": 7.5},
        headers=_headers(admin),
    )
    assert create.status_code == 201, create.text
    rate_id = create.json()["id"]
    assert create.json()["amount"] == 7.5

    upd = await client.put(
        f"/api/v1/admin/shipping/rates/{rate_id}",
        json={"amount": 9.0, "free_above_subtotal": 100},
        headers=_headers(admin),
    )
    assert upd.status_code == 200
    assert upd.json()["amount"] == 9.0
    assert upd.json()["free_above_subtotal"] == 100

    deleted = await client.delete(
        f"/api/v1/admin/shipping/rates/{rate_id}", headers=_headers(admin)
    )
    assert deleted.status_code == 204

    rows = (
        db_session.query(AdminAuditEvent)
        .filter(AdminAuditEvent.resource_type == "shipping_rate")
        .order_by(AdminAuditEvent.id)
        .all()
    )
    assert [r.action for r in rows] == ["create", "update", "delete"]


async def test_rate_combo_uniqueness_400(client, user_factory):
    admin = user_factory(email="ship-rate-dup@example.com", role=UserRole.ADMIN)
    z, m = await _seed_zone_and_method(
        client, admin, zone_name="Dup Combo Zone", country="LU", method_code="dup-combo"
    )
    a = await client.post(
        "/api/v1/admin/shipping/rates",
        json={"zone_id": z, "method_id": m, "currency": "EUR", "amount": 5},
        headers=_headers(admin),
    )
    assert a.status_code == 201
    b = await client.post(
        "/api/v1/admin/shipping/rates",
        json={"zone_id": z, "method_id": m, "currency": "EUR", "amount": 6},
        headers=_headers(admin),
    )
    assert b.status_code == 400
    assert "already exists" in b.json()["detail"].lower()


async def test_rate_create_404_on_missing_zone(client, user_factory):
    admin = user_factory(email="ship-rate-404@example.com", role=UserRole.ADMIN)
    # Need a real method but a bogus zone.
    m = await client.post(
        "/api/v1/admin/shipping/methods",
        json={"name": "M for missing zone test", "code": "m-mzt"},
        headers=_headers(admin),
    )
    method_id = m.json()["id"]
    resp = await client.post(
        "/api/v1/admin/shipping/rates",
        json={"zone_id": 999_999, "method_id": method_id, "currency": "USD", "amount": 1},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


async def test_rates_list_filter_by_currency(client, user_factory):
    admin = user_factory(email="ship-rate-filter@example.com", role=UserRole.ADMIN)
    z, m = await _seed_zone_and_method(
        client, admin, zone_name="Filter Zone", country="SI", method_code="filter-method"
    )
    for currency, amount in [("USD", 5), ("PLN", 20), ("EUR", 4.5)]:
        r = await client.post(
            "/api/v1/admin/shipping/rates",
            json={"zone_id": z, "method_id": m, "currency": currency, "amount": amount},
            headers=_headers(admin),
        )
        assert r.status_code == 201

    eur = await client.get(
        f"/api/v1/admin/shipping/rates?zone_id={z}&method_id={m}&currency=EUR",
        headers=_headers(admin),
    )
    assert eur.status_code == 200
    items = eur.json()["items"]
    # Could include the session-seeded test rate if its (zone, method) overlap; filter to ours.
    ours = [x for x in items if x["zone_id"] == z and x["method_id"] == m]
    assert len(ours) == 1
    assert ours[0]["currency"] == "EUR"
    assert ours[0]["amount"] == 4.5


async def test_rate_create_with_tiers_persisted_sorted(client, user_factory):
    admin = user_factory(email="ship-rate-tier@example.com", role=UserRole.ADMIN)
    z, m = await _seed_zone_and_method(
        client, admin, zone_name="Tier Zone", country="EE", method_code="tier-method"
    )
    # Tiers intentionally out-of-order in the request body.
    resp = await client.post(
        "/api/v1/admin/shipping/rates",
        json={
            "zone_id": z,
            "method_id": m,
            "currency": "EUR",
            "amount": 19.99,
            "tiers": [
                {"min_subtotal": 500, "amount": 4.99},
                {"min_subtotal": 0, "amount": 19.99},
                {"min_subtotal": 200, "amount": 9.99},
            ],
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    tiers = resp.json()["tiers"]
    # The CRUD canonicalised them sorted ascending by min_subtotal.
    assert [t["min_subtotal"] for t in tiers] == [0, 200, 500]
    assert [t["amount"] for t in tiers] == [19.99, 9.99, 4.99]


async def test_rate_create_tiers_dup_min_subtotal_422(client, user_factory):
    admin = user_factory(email="ship-rate-tier-dup@example.com", role=UserRole.ADMIN)
    z, m = await _seed_zone_and_method(
        client, admin, zone_name="Tier Dup Zone", country="LV", method_code="tier-dup-method"
    )
    resp = await client.post(
        "/api/v1/admin/shipping/rates",
        json={
            "zone_id": z,
            "method_id": m,
            "currency": "EUR",
            "amount": 19.99,
            "tiers": [
                {"min_subtotal": 0, "amount": 10},
                {"min_subtotal": 0, "amount": 5},
            ],
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 422
