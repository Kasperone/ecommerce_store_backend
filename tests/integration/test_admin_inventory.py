"""Integration tests for the admin inventory API (/api/v1/admin/inventory).

Covers authz, the stock list + status/search filters, manual adjust (delta
computed, movement + audit row written, no-op + negative rejected, 404), the
movement log, and the order place/cancel paths landing on the same ledger.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.crud import inventory as crud_inventory
from app.models.admin_audit_event import AdminAuditEvent
from app.models.inventory_movement import InventoryMovementReason
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _order_body(product_id: int, quantity: int = 1, currency: str = "USD") -> dict:
    return {
        "items": [{"product_id": product_id, "quantity": quantity}],
        "currency": currency,
        "shipping_address": "1 Test St",
        "shipping_city": "Testville",
        "shipping_postal_code": "00-001",
        "shipping_country": "Poland",
    }


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_list_requires_admin(client, user_factory):
    customer = user_factory(email="inv-cust@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/inventory/")).status_code == 401
    assert (
        await client.get("/api/v1/admin/inventory/", headers=_headers(customer))
    ).status_code == 403


async def test_movements_requires_admin(client, user_factory):
    customer = user_factory(email="inv-cust2@example.com", role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/inventory/movements")).status_code == 401
    assert (
        await client.get("/api/v1/admin/inventory/movements", headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# list + filters
# ---------------------------------------------------------------------------


async def test_list_with_status_and_search(client, user_factory, product_factory):
    admin = user_factory(email="inv-admin@example.com", role=UserRole.ADMIN)
    out = product_factory(stock=0, name="Zinc Bolt")
    low = product_factory(stock=2, low_stock_threshold=5, name="Yellow Tag")
    high = product_factory(stock=99, name="Xenon Lamp")

    all_resp = await client.get("/api/v1/admin/inventory/", headers=_headers(admin))
    assert all_resp.status_code == 200
    assert all_resp.json()["total"] == 3
    statuses = {r["name"]: r["status"] for r in all_resp.json()["items"]}
    assert statuses["Zinc Bolt"] == "out_of_stock"
    assert statuses["Yellow Tag"] == "low_stock"
    assert statuses["Xenon Lamp"] == "in_stock"

    low_resp = await client.get(
        "/api/v1/admin/inventory/?status=low_stock", headers=_headers(admin)
    )
    assert {r["id"] for r in low_resp.json()["items"]} == {low.id}

    out_resp = await client.get(
        "/api/v1/admin/inventory/?status=out_of_stock", headers=_headers(admin)
    )
    assert {r["id"] for r in out_resp.json()["items"]} == {out.id}

    search_resp = await client.get("/api/v1/admin/inventory/?search=xenon", headers=_headers(admin))
    assert {r["id"] for r in search_resp.json()["items"]} == {high.id}


# ---------------------------------------------------------------------------
# manual adjust
# ---------------------------------------------------------------------------


async def test_adjust_sets_absolute_records_movement_and_audit(
    client, user_factory, product_factory, db_session: Session
):
    admin = user_factory(email="adj-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, name="Adjustable")

    resp = await client.patch(
        f"/api/v1/admin/inventory/products/{product.id}/stock",
        json={"new_stock": 42, "note": "recount"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["delta"] == 32
    assert body["resulting_stock"] == 42
    assert body["reason"] == "manual_adjustment"
    assert body["note"] == "recount"
    assert body["product_name"] == "Adjustable"
    assert body["actor_user_id"] == admin.id

    audit = (
        db_session.query(AdminAuditEvent)
        .filter(
            AdminAuditEvent.resource_type == "inventory",
            AdminAuditEvent.resource_id == product.id,
            AdminAuditEvent.action == "update",
        )
        .count()
    )
    assert audit == 1


async def test_adjust_noop_rejected(client, user_factory, product_factory):
    admin = user_factory(email="noop-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=7)
    resp = await client.patch(
        f"/api/v1/admin/inventory/products/{product.id}/stock",
        json={"new_stock": 7},
        headers=_headers(admin),
    )
    assert resp.status_code == 400


async def test_adjust_negative_rejected(client, user_factory, product_factory):
    admin = user_factory(email="neg-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=7)
    resp = await client.patch(
        f"/api/v1/admin/inventory/products/{product.id}/stock",
        json={"new_stock": -1},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_adjust_missing_product_404(client, user_factory):
    admin = user_factory(email="404-admin@example.com", role=UserRole.ADMIN)
    resp = await client.patch(
        "/api/v1/admin/inventory/products/999999/stock",
        json={"new_stock": 5},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# movement log + order integration
# ---------------------------------------------------------------------------


async def test_order_place_and_cancel_land_on_ledger(client, user_factory, product_factory):
    admin = user_factory(email="ledger-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="ledger-cust@example.com")
    product = product_factory(stock=10, price_usd=10.0, name="Ledgered")

    created = await client.post(
        "/api/v1/orders/", json=_order_body(product.id, 3), headers=_headers(customer)
    )
    assert created.status_code == 201
    order_id = created.json()["id"]

    log = await client.get(
        f"/api/v1/admin/inventory/movements?product_id={product.id}", headers=_headers(admin)
    )
    assert log.status_code == 200
    items = log.json()["items"]
    assert len(items) == 1
    assert items[0]["reason"] == "order_placed"
    assert items[0]["delta"] == -3
    assert items[0]["resulting_stock"] == 7
    assert items[0]["order_id"] == order_id
    assert items[0]["actor_user_id"] == customer.id

    cancel = await client.post(f"/api/v1/orders/{order_id}/cancel", headers=_headers(customer))
    assert cancel.status_code == 200

    log2 = await client.get(
        f"/api/v1/admin/inventory/movements?product_id={product.id}", headers=_headers(admin)
    )
    rows = log2.json()["items"]
    assert [r["reason"] for r in rows] == ["order_cancelled", "order_placed"]  # newest first
    assert rows[0]["delta"] == 3
    assert rows[0]["resulting_stock"] == 10
    assert rows[0]["actor_user_id"] == customer.id


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------


async def test_list_pagination(client, user_factory, product_factory):
    admin = user_factory(email="page-admin@example.com", role=UserRole.ADMIN)
    for i in range(25):
        product_factory(name=f"Bulk {i:02d}")

    p1 = await client.get("/api/v1/admin/inventory/?page=1&page_size=20", headers=_headers(admin))
    assert p1.status_code == 200
    body1 = p1.json()
    assert body1["total"] == 25
    assert body1["pages"] == 2
    assert body1["page"] == 1
    assert len(body1["items"]) == 20

    p2 = await client.get("/api/v1/admin/inventory/?page=2&page_size=20", headers=_headers(admin))
    assert len(p2.json()["items"]) == 5


async def test_movements_pagination(client, user_factory, product_factory, db_session):
    admin = user_factory(email="page-mv-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=100)
    for _ in range(25):
        crud_inventory.apply_delta(
            db_session,
            product=product,
            delta=-1,
            reason=InventoryMovementReason.MANUAL_ADJUSTMENT,
        )
    db_session.commit()

    p1 = await client.get(
        "/api/v1/admin/inventory/movements?page=1&page_size=20", headers=_headers(admin)
    )
    assert p1.status_code == 200
    body1 = p1.json()
    assert body1["total"] == 25
    assert body1["pages"] == 2
    assert len(body1["items"]) == 20

    p2 = await client.get(
        "/api/v1/admin/inventory/movements?page=2&page_size=20", headers=_headers(admin)
    )
    assert len(p2.json()["items"]) == 5
