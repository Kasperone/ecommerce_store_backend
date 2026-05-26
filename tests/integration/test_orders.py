"""Integration tests for the orders route layer.

Covers create (with stock decrement + pricing), scoped list/detail, the admin
PATCH status workflow, and the cancel endpoint. Foundational orders plan:
``ecommerce_store/.claude/notes/plans/orders/in-progress/2026-05-25-orders-foundational.md``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.inventory_movement import InventoryMovement
from app.models.product import Product
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
# POST /  (create)
# ---------------------------------------------------------------------------


async def test_create_order_happy_path(client, user_factory, product_factory, db_session):
    user = user_factory()
    product = product_factory(stock=10, price_usd=25.0)

    resp = await client.post(
        "/api/v1/orders/", json=_order_body(product.id, 2), headers=_headers(user)
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["subtotal"] == 50.0
    assert body["total"] == 50.0
    assert body["order_number"].startswith("ORD-")
    assert len(body["items"]) == 1

    # Stock decremented in the DB.
    fresh = db_session.get(Product, product.id)
    db_session.refresh(fresh)
    assert fresh.stock == 8

    # The placement landed on the inventory ledger.
    movements = db_session.query(InventoryMovement).filter_by(product_id=product.id).all()
    assert len(movements) == 1
    assert movements[0].reason == "order_placed"
    assert movements[0].delta == -2


async def test_create_order_insufficient_stock_422(client, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=1, price_usd=25.0)

    resp = await client.post(
        "/api/v1/orders/", json=_order_body(product.id, 5), headers=_headers(user)
    )
    assert resp.status_code == 422
    assert "stock" in resp.json()["detail"].lower()


async def test_create_order_inactive_product_422(client, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=25.0, is_active=False)

    resp = await client.post(
        "/api/v1/orders/", json=_order_body(product.id, 1), headers=_headers(user)
    )
    assert resp.status_code == 422


async def test_create_order_requires_auth(client, product_factory):
    product = product_factory(stock=10, price_usd=25.0)
    resp = await client.post("/api/v1/orders/", json=_order_body(product.id, 1))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /  (scoped list)
# ---------------------------------------------------------------------------


async def test_list_is_scoped_per_user(client, user_factory, product_factory):
    owner = user_factory(email="list-owner@example.com")
    other = user_factory(email="list-other@example.com")
    product = product_factory(stock=20, price_usd=10.0)

    await client.post("/api/v1/orders/", json=_order_body(product.id, 1), headers=_headers(owner))
    await client.post("/api/v1/orders/", json=_order_body(product.id, 1), headers=_headers(other))

    owner_list = await client.get("/api/v1/orders/", headers=_headers(owner))
    assert owner_list.status_code == 200
    assert owner_list.json()["total"] == 1
    # List rows carry the lightweight shape (item_count, no nested items array).
    row = owner_list.json()["items"][0]
    assert row["item_count"] == 1
    assert "items" not in row


async def test_admin_list_sees_all_with_customer_email(client, user_factory, product_factory):
    owner = user_factory(email="seen@example.com")
    admin = user_factory(email="admin-list@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=20, price_usd=10.0)
    await client.post("/api/v1/orders/", json=_order_body(product.id, 1), headers=_headers(owner))

    resp = await client.get("/api/v1/orders/", headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["customer_email"] == "seen@example.com"


async def test_list_status_filter(client, user_factory, product_factory):
    admin = user_factory(email="admin-filter@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="filter-cust@example.com")
    product = product_factory(stock=20, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/", json=_order_body(product.id, 1), headers=_headers(customer)
    )
    order_id = created.json()["id"]
    await client.post(f"/api/v1/orders/{order_id}/cancel", headers=_headers(customer))

    pending = await client.get("/api/v1/orders/?status=pending", headers=_headers(admin))
    cancelled = await client.get("/api/v1/orders/?status=cancelled", headers=_headers(admin))
    assert pending.json()["total"] == 0
    assert cancelled.json()["total"] == 1


# ---------------------------------------------------------------------------
# GET /{id}  (detail, ownership)
# ---------------------------------------------------------------------------


async def test_detail_ownership(client, user_factory, product_factory):
    owner = user_factory(email="d-owner@example.com")
    other = user_factory(email="d-other@example.com")
    admin = user_factory(email="d-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/", json=_order_body(product.id, 1), headers=_headers(owner)
    )
    order_id = created.json()["id"]

    url = f"/api/v1/orders/{order_id}"
    assert (await client.get(url, headers=_headers(owner))).status_code == 200
    # Cross-customer is 404 (existence not leaked), admin sees any.
    assert (await client.get(url, headers=_headers(other))).status_code == 404
    assert (await client.get(url, headers=_headers(admin))).status_code == 200


# ---------------------------------------------------------------------------
# POST /{id}/cancel
# ---------------------------------------------------------------------------


async def test_customer_cancels_pending_and_restores_stock(
    client, user_factory, product_factory, db_session
):
    user = user_factory(email="cancel@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/", json=_order_body(product.id, 4), headers=_headers(user)
    )
    order_id = created.json()["id"]

    resp = await client.post(f"/api/v1/orders/{order_id}/cancel", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    fresh = db_session.get(Product, product.id)
    db_session.refresh(fresh)
    assert fresh.stock == 10  # restored


async def test_customer_cannot_cancel_non_pending(client, user_factory, product_factory):
    customer = user_factory(email="np-cust@example.com")
    admin = user_factory(email="np-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/", json=_order_body(product.id, 1), headers=_headers(customer)
    )
    order_id = created.json()["id"]
    # Admin advances it past pending.
    await client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "paid"}, headers=_headers(admin)
    )

    resp = await client.post(f"/api/v1/orders/{order_id}/cancel", headers=_headers(customer))
    assert resp.status_code == 400


async def test_cancel_cross_customer_404(client, user_factory, product_factory):
    owner = user_factory(email="cc-owner@example.com")
    other = user_factory(email="cc-other@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/", json=_order_body(product.id, 1), headers=_headers(owner)
    )
    order_id = created.json()["id"]

    resp = await client.post(f"/api/v1/orders/{order_id}/cancel", headers=_headers(other))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /{id}  (admin status workflow)
# ---------------------------------------------------------------------------


async def test_admin_patch_valid_transition_and_audit(
    client, user_factory, product_factory, db_session: Session
):
    customer = user_factory(email="patch-cust@example.com")
    admin = user_factory(email="patch-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/", json=_order_body(product.id, 1), headers=_headers(customer)
    )
    order_id = created.json()["id"]

    resp = await client.patch(
        f"/api/v1/orders/{order_id}",
        json={"status": "paid", "payment_method": "stripe"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "paid"
    assert resp.json()["payment_method"] == "stripe"

    audit = (
        db_session.query(AdminAuditEvent)
        .filter(
            AdminAuditEvent.resource_type == "order",
            AdminAuditEvent.resource_id == order_id,
            AdminAuditEvent.action == "update",
        )
        .count()
    )
    assert audit == 1


async def test_admin_patch_invalid_transition_400(client, user_factory, product_factory):
    customer = user_factory(email="bad-cust@example.com")
    admin = user_factory(email="bad-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/", json=_order_body(product.id, 1), headers=_headers(customer)
    )
    order_id = created.json()["id"]

    # pending -> delivered skips the workflow.
    resp = await client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "delivered"}, headers=_headers(admin)
    )
    assert resp.status_code == 400


async def test_admin_patch_to_cancelled_rejected(client, user_factory, product_factory):
    customer = user_factory(email="patchcancel-cust@example.com")
    admin = user_factory(email="patchcancel-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/", json=_order_body(product.id, 1), headers=_headers(customer)
    )
    order_id = created.json()["id"]

    resp = await client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "cancelled"}, headers=_headers(admin)
    )
    assert resp.status_code == 400
    assert "cancel endpoint" in resp.json()["detail"]


async def test_customer_cannot_patch(client, user_factory, product_factory):
    customer = user_factory(email="forbidden@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/", json=_order_body(product.id, 1), headers=_headers(customer)
    )
    order_id = created.json()["id"]

    resp = await client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "paid"}, headers=_headers(customer)
    )
    assert resp.status_code == 403
