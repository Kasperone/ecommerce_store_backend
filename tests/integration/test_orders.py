"""Integration tests for the orders route layer.

Covers create (with stock decrement + pricing), scoped list/detail, the admin
PATCH status workflow, and the cancel endpoint. Foundational orders plan:
``ecommerce_store/.claude/notes/plans/orders/in-progress/2026-05-25-orders-foundational.md``.
"""

from __future__ import annotations

from decimal import Decimal

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


def _order_body(
    product_id: int, *, shipping_method_id: int, quantity: int = 1, currency: str = "USD"
) -> dict:
    return {
        "items": [{"product_id": product_id, "quantity": quantity}],
        "currency": currency,
        "shipping_address": "1 Test St",
        "shipping_city": "Testville",
        "shipping_postal_code": "00-001",
        "shipping_country": "PL",
        "shipping_method_id": shipping_method_id,
    }


# ---------------------------------------------------------------------------
# POST /  (create)
# ---------------------------------------------------------------------------


async def test_create_order_happy_path(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=25.0)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=2, shipping_method_id=test_shipping_method_id),
        headers=_headers(user),
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


async def test_create_order_insufficient_stock_422(
    client, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=1, price_usd=25.0)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=5, shipping_method_id=test_shipping_method_id),
        headers=_headers(user),
    )
    assert resp.status_code == 422
    assert "stock" in resp.json()["detail"].lower()


async def test_create_order_inactive_product_422(
    client, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=25.0, is_active=False)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(user),
    )
    assert resp.status_code == 422


async def test_create_order_anonymous_without_guest_email_422(
    client, product_factory, test_shipping_method_id
):
    # Auth is optional since guest checkout — but an anonymous create without a
    # guest_email has no owner and no contact, so it's rejected (was a 401).
    product = product_factory(stock=10, price_usd=25.0)
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
    )
    assert resp.status_code == 422
    assert "guest_email" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# POST /  (create — purchasable gift-card products, gift cards v2)
# ---------------------------------------------------------------------------


async def test_create_all_gift_card_order_skips_shipping(
    client, user_factory, product_factory, test_shipping_method_id
):
    """An all-gift-card cart is placeable with NO shipping method/country/address."""
    user = user_factory()
    card_product = product_factory(is_gift_card=True, stock=100, price_usd=50.0)
    resp = await client.post(
        "/api/v1/orders/",
        json={
            "items": [
                {
                    "product_id": card_product.id,
                    "quantity": 1,
                    "gift_card_recipient_email": "friend@example.com",
                    "gift_card_message": "Enjoy!",
                }
            ],
            "currency": "USD",
        },
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["total"] == 50.0
    assert body["shipping"] == 0.0
    assert body["shipping_method_id"] is None
    line = body["items"][0]
    assert line["is_gift_card"] is True
    assert line["gift_card_recipient_email"] == "friend@example.com"


async def test_create_physical_order_without_shipping_422(client, user_factory, product_factory):
    """A physical line with no shipping details is rejected (ShippingRequiredError)."""
    user = user_factory()
    product = product_factory(is_gift_card=False, stock=10, price_usd=25.0)
    resp = await client.post(
        "/api/v1/orders/",
        json={
            "items": [{"product_id": product.id, "quantity": 1}],
            "currency": "USD",
        },
        headers=_headers(user),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /  (create — weight-tier resolution via crud_order.create_with_items)
# ---------------------------------------------------------------------------


def _enable_weight_tiers_on_seeded_rate(
    db_session: Session,
    *,
    method_id: int,
    currency: str = "USD",
    bands: list[dict[str, float]] | None = None,
) -> None:
    """Stamp weight_tiers onto the seeded ``Test Shipping`` rate for one test.

    The savepoint rollback at teardown reverts this so other tests see the
    seeded $0 / no-tiers shape. Default bands match the resolver test matrix.
    """
    from app.models.shipping import ShippingRate

    if bands is None:
        bands = [
            {"max_weight_kg": 1.0, "amount": 9.99},
            {"max_weight_kg": 5.0, "amount": 19.99},
            {"max_weight_kg": 30.0, "amount": 49.99},
        ]
    rate = (
        db_session.query(ShippingRate)
        .filter(ShippingRate.method_id == method_id, ShippingRate.currency == currency)
        .first()
    )
    assert rate is not None, "Seeded Test Shipping rate missing"
    rate.amount = Decimal("99.99")
    rate.weight_tiers = bands
    db_session.commit()


async def test_create_order_resolves_weight_tier_band(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    """Cart weight = 2 * 1.5 kg = 3 kg → band 2 (max_weight_kg=5) → $19.99."""
    _enable_weight_tiers_on_seeded_rate(db_session, method_id=test_shipping_method_id)
    user = user_factory()
    product = product_factory(stock=10, price_usd=25.0, weight_kg=1.5)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=2, shipping_method_id=test_shipping_method_id),
        headers=_headers(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["subtotal"] == 50.0
    assert body["shipping"] == 19.99
    assert body["total"] == 69.99


async def test_create_order_null_weight_falls_through_to_flat(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    """D2: legacy product with NULL weight contributes 0 → weight branch skipped.

    The seeded rate has weight_tiers but no subtotal tiers, so the cart falls
    through to ``rate.amount`` (the flat $99.99 fallback set by the helper).
    """
    _enable_weight_tiers_on_seeded_rate(db_session, method_id=test_shipping_method_id)
    user = user_factory()
    product = product_factory(stock=10, price_usd=25.0, weight_kg=None)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["shipping"] == 99.99


async def test_create_order_mixed_weight_lines_sums_to_band(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    """Mixed cart: NULL weight contributes 0, weighted lines sum normally.

    1x null-weight + 3x 0.2 kg = 0.6 kg -> band 1 (max_weight_kg=1.0) -> $9.99.
    """
    _enable_weight_tiers_on_seeded_rate(db_session, method_id=test_shipping_method_id)
    user = user_factory()
    legacy = product_factory(stock=10, price_usd=10.0, weight_kg=None)
    small = product_factory(stock=10, price_usd=5.0, weight_kg=0.2)

    resp = await client.post(
        "/api/v1/orders/",
        json={
            "items": [
                {"product_id": legacy.id, "quantity": 1},
                {"product_id": small.id, "quantity": 3},
            ],
            "currency": "USD",
            "shipping_address": "1 Test St",
            "shipping_city": "Testville",
            "shipping_postal_code": "00-001",
            "shipping_country": "PL",
            "shipping_method_id": test_shipping_method_id,
        },
        headers=_headers(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["subtotal"] == 25.0  # 10 + 3 * 5
    assert body["shipping"] == 9.99


# ---------------------------------------------------------------------------
# GET /  (scoped list)
# ---------------------------------------------------------------------------


async def test_list_is_scoped_per_user(
    client, user_factory, product_factory, test_shipping_method_id
):
    owner = user_factory(email="list-owner@example.com")
    other = user_factory(email="list-other@example.com")
    product = product_factory(stock=20, price_usd=10.0)

    await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(owner),
    )
    await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(other),
    )

    owner_list = await client.get("/api/v1/orders/", headers=_headers(owner))
    assert owner_list.status_code == 200
    assert owner_list.json()["total"] == 1
    # List rows carry the lightweight shape (item_count, no nested items array).
    row = owner_list.json()["items"][0]
    assert row["item_count"] == 1
    assert "items" not in row


async def test_admin_list_sees_all_with_customer_email(
    client, user_factory, product_factory, test_shipping_method_id
):
    owner = user_factory(email="seen@example.com")
    admin = user_factory(email="admin-list@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=20, price_usd=10.0)
    await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(owner),
    )

    resp = await client.get("/api/v1/orders/", headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["customer_email"] == "seen@example.com"


async def test_list_status_filter(client, user_factory, product_factory, test_shipping_method_id):
    admin = user_factory(email="admin-filter@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="filter-cust@example.com")
    product = product_factory(stock=20, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(customer),
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


async def test_detail_ownership(client, user_factory, product_factory, test_shipping_method_id):
    owner = user_factory(email="d-owner@example.com")
    other = user_factory(email="d-other@example.com")
    admin = user_factory(email="d-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(owner),
    )
    order_id = created.json()["id"]

    url = f"/api/v1/orders/{order_id}"
    assert (await client.get(url, headers=_headers(owner))).status_code == 200
    # Cross-customer is 404 (existence not leaked), admin sees any.
    assert (await client.get(url, headers=_headers(other))).status_code == 404
    assert (await client.get(url, headers=_headers(admin))).status_code == 200


async def test_detail_with_anonymized_owner_does_not_500(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    """S1 regression: an order whose owner was GDPR-anonymized must still open.

    The anonymizer rewrites the owner's email to the reserved ``.invalid`` TLD,
    which ``EmailStr`` rejects — the nested-user response schema relaxes it to a
    plain ``str`` so serialization returns 200 instead of a 500.
    """
    from app.crud.user import user as crud_user

    owner = user_factory(email="anon-owner@example.com")
    admin = user_factory(email="anon-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(owner),
    )
    order_id = created.json()["id"]

    crud_user.anonymize(db_session, user=owner)
    db_session.commit()
    assert owner.email == f"deleted-user-{owner.id}@anon.invalid"

    resp = await client.get(f"/api/v1/orders/{order_id}", headers=_headers(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["email"] == f"deleted-user-{owner.id}@anon.invalid"
    # Anonymizer placeholders stay visible so the admin sees why the row is empty.
    assert body["user"]["first_name"] == "deleted"
    assert body["user"]["last_name"] == "user"


# ---------------------------------------------------------------------------
# POST /{id}/cancel
# ---------------------------------------------------------------------------


async def test_customer_cancels_pending_and_restores_stock(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    user = user_factory(email="cancel@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=4, shipping_method_id=test_shipping_method_id),
        headers=_headers(user),
    )
    order_id = created.json()["id"]

    resp = await client.post(f"/api/v1/orders/{order_id}/cancel", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    fresh = db_session.get(Product, product.id)
    db_session.refresh(fresh)
    assert fresh.stock == 10  # restored


async def test_customer_cannot_cancel_non_pending(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="np-cust@example.com")
    admin = user_factory(email="np-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(customer),
    )
    order_id = created.json()["id"]
    # Admin advances it past pending.
    await client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "paid"}, headers=_headers(admin)
    )

    resp = await client.post(f"/api/v1/orders/{order_id}/cancel", headers=_headers(customer))
    assert resp.status_code == 400


async def test_cancel_cross_customer_404(
    client, user_factory, product_factory, test_shipping_method_id
):
    owner = user_factory(email="cc-owner@example.com")
    other = user_factory(email="cc-other@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(owner),
    )
    order_id = created.json()["id"]

    resp = await client.post(f"/api/v1/orders/{order_id}/cancel", headers=_headers(other))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /{id}  (admin status workflow)
# ---------------------------------------------------------------------------


async def test_admin_patch_valid_transition_and_audit(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    customer = user_factory(email="patch-cust@example.com")
    admin = user_factory(email="patch-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(customer),
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


async def test_admin_patch_invalid_transition_400(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="bad-cust@example.com")
    admin = user_factory(email="bad-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(customer),
    )
    order_id = created.json()["id"]

    # pending -> delivered skips the workflow.
    resp = await client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "delivered"}, headers=_headers(admin)
    )
    assert resp.status_code == 400


async def test_admin_patch_to_cancelled_rejected(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="patchcancel-cust@example.com")
    admin = user_factory(email="patchcancel-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(customer),
    )
    order_id = created.json()["id"]

    resp = await client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "cancelled"}, headers=_headers(admin)
    )
    assert resp.status_code == 400
    assert "cancel endpoint" in resp.json()["detail"]


async def test_customer_cannot_patch(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="forbidden@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(customer),
    )
    order_id = created.json()["id"]

    resp = await client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "paid"}, headers=_headers(customer)
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Shipment tracking (admin PATCH + customer exposure)
# ---------------------------------------------------------------------------


async def _create_order_id(client, customer, product, shipping_method_id: int) -> int:
    created = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=shipping_method_id),
        headers=_headers(customer),
    )
    assert created.status_code == 201
    return created.json()["id"]


async def test_admin_sets_tracking_on_ship_and_audit_records_it(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    customer = user_factory(email="track-cust@example.com")
    admin = user_factory(email="track-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    order_id = await _create_order_id(client, customer, product, test_shipping_method_id)
    await client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "paid"}, headers=_headers(admin)
    )

    resp = await client.patch(
        f"/api/v1/orders/{order_id}",
        json={
            "status": "shipped",
            "shipping_carrier": "DHL",
            "tracking_number": "JD014600003SE28X",
            "tracking_url": "https://track.example.com/JD014600003SE28X",
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "shipped"
    assert body["shipping_carrier"] == "DHL"
    assert body["tracking_number"] == "JD014600003SE28X"
    assert body["tracking_url"] == "https://track.example.com/JD014600003SE28X"

    # The audit diff carries the tracking fields like any other admin change.
    event = (
        db_session.query(AdminAuditEvent)
        .filter(
            AdminAuditEvent.resource_type == "order",
            AdminAuditEvent.resource_id == order_id,
            AdminAuditEvent.action == "update",
        )
        .order_by(AdminAuditEvent.id.desc())
        .first()
    )
    assert event is not None
    assert event.payload["after"]["shipping_carrier"] == "DHL"
    assert event.payload["after"]["tracking_number"] == "JD014600003SE28X"
    assert event.payload["after"]["tracking_url"] == "https://track.example.com/JD014600003SE28X"
    assert event.payload["before"]["tracking_number"] is None


async def test_admin_edits_tracking_after_ship_without_status(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="trackedit-cust@example.com")
    admin = user_factory(email="trackedit-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    order_id = await _create_order_id(client, customer, product, test_shipping_method_id)
    await client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "paid"}, headers=_headers(admin)
    )
    await client.patch(
        f"/api/v1/orders/{order_id}",
        json={"status": "shipped", "shipping_carrier": "DHL", "tracking_number": "WRONG-1"},
        headers=_headers(admin),
    )

    # Tracking-only PATCH (no status) corrects the number and adds the URL.
    resp = await client.patch(
        f"/api/v1/orders/{order_id}",
        json={
            "tracking_number": "  JD014600003SE28X  ",  # trimmed
            "tracking_url": "https://track.example.com/JD014600003SE28X",
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "shipped"  # unchanged
    assert body["shipping_carrier"] == "DHL"  # untouched (not in the PATCH)
    assert body["tracking_number"] == "JD014600003SE28X"
    assert body["tracking_url"] == "https://track.example.com/JD014600003SE28X"


async def test_admin_blank_tracking_value_clears_to_null(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="trackclear-cust@example.com")
    admin = user_factory(email="trackclear-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    order_id = await _create_order_id(client, customer, product, test_shipping_method_id)
    await client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "paid"}, headers=_headers(admin)
    )
    await client.patch(
        f"/api/v1/orders/{order_id}",
        json={
            "status": "shipped",
            "shipping_carrier": "DHL",
            "tracking_number": "JD014600003SE28X",
            "tracking_url": "https://track.example.com/JD014600003SE28X",
        },
        headers=_headers(admin),
    )

    resp = await client.patch(
        f"/api/v1/orders/{order_id}",
        json={"tracking_url": "   "},  # whitespace-only → cleared
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tracking_url"] is None
    assert body["tracking_number"] == "JD014600003SE28X"  # untouched


async def test_customer_sees_tracking_on_detail_and_list(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="trackview-cust@example.com")
    admin = user_factory(email="trackview-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    order_id = await _create_order_id(client, customer, product, test_shipping_method_id)
    await client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "paid"}, headers=_headers(admin)
    )
    await client.patch(
        f"/api/v1/orders/{order_id}",
        json={
            "status": "shipped",
            "shipping_carrier": "DHL",
            "tracking_number": "JD014600003SE28X",
            "tracking_url": "https://track.example.com/JD014600003SE28X",
        },
        headers=_headers(admin),
    )

    detail = await client.get(f"/api/v1/orders/{order_id}", headers=_headers(customer))
    assert detail.status_code == 200
    body = detail.json()
    assert body["shipping_carrier"] == "DHL"
    assert body["tracking_number"] == "JD014600003SE28X"
    assert body["tracking_url"] == "https://track.example.com/JD014600003SE28X"

    listed = await client.get("/api/v1/orders/", headers=_headers(customer))
    assert listed.status_code == 200
    row = next(item for item in listed.json()["items"] if item["id"] == order_id)
    assert row["shipping_carrier"] == "DHL"
    assert row["tracking_number"] == "JD014600003SE28X"
    assert row["tracking_url"] == "https://track.example.com/JD014600003SE28X"


# ---------------------------------------------------------------------------
# POST /  (create — variant lines, PR-B2)
# ---------------------------------------------------------------------------


def _add_variants(db_session, product, *, stocks=(4, 6), price_usd=(None, None)):
    """Author a one-option (Red|Blue) matrix on ``product``; see the unit-test twin."""
    from app.crud import product_variant as crud_product_variant
    from app.schemas.product_variant import ProductOptionsMatrixUpdate

    matrix = ProductOptionsMatrixUpdate.model_validate(
        {
            "options": [
                {
                    "name": "Color",
                    "position": 0,
                    "values": [
                        {"value": "Red", "position": 0},
                        {"value": "Blue", "position": 1},
                    ],
                }
            ],
            "variants": [
                {
                    "option_values": ["Red"],
                    "sku": f"VI-RED-{product.id}",
                    "initial_stock": stocks[0],
                    "price_usd": price_usd[0],
                    "position": 0,
                },
                {
                    "option_values": ["Blue"],
                    "sku": f"VI-BLUE-{product.id}",
                    "initial_stock": stocks[1],
                    "price_usd": price_usd[1],
                    "position": 1,
                },
            ],
        }
    )
    crud_product_variant.apply_matrix(db_session, product=product, matrix=matrix)
    db_session.commit()
    db_session.refresh(product)
    return crud_product_variant.get_for_product(db_session, product_id=product.id)


def _variant_order_body(
    product_id: int, variant_id: int | None, *, shipping_method_id: int, quantity: int = 1
) -> dict:
    body = _order_body(product_id, shipping_method_id=shipping_method_id, quantity=quantity)
    body["items"][0]["variant_id"] = variant_id
    return body


async def test_create_order_variant_line_happy_path(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=0, price_usd=20.0)
    red, _blue = _add_variants(db_session, product, price_usd=(25.0, None))

    resp = await client.post(
        "/api/v1/orders/",
        json=_variant_order_body(
            product.id, red.id, quantity=2, shipping_method_id=test_shipping_method_id
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    line = body["items"][0]
    # The response carries the additive variant fields (reorder contract).
    assert line["variant_id"] == red.id
    assert line["variant_title"] == "Red"
    assert line["product_sku"] == f"VI-RED-{product.id}"
    assert line["unit_price"] == 25.0
    assert body["subtotal"] == 50.0

    db_session.refresh(red)
    db_session.refresh(product)
    assert red.stock == 2
    assert product.stock == 8
    movement = (
        db_session.query(InventoryMovement)
        .filter_by(product_id=product.id, reason="order_placed")
        .one()
    )
    assert movement.variant_id == red.id
    assert movement.delta == -2


async def test_create_order_variant_required_422(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=0, price_usd=20.0)
    _add_variants(db_session, product)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(user),
    )
    assert resp.status_code == 422
    assert "variant" in resp.json()["detail"].lower()


async def test_create_order_variant_unavailable_422(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    from app.crud import product_variant as crud_product_variant
    from app.schemas.product_variant import ProductVariantUpdate

    user = user_factory()
    product = product_factory(stock=0, price_usd=20.0)
    red, _blue = _add_variants(db_session, product)
    crud_product_variant.update_variant(
        db_session, variant=red, obj_in=ProductVariantUpdate(is_active=False)
    )
    db_session.commit()

    resp = await client.post(
        "/api/v1/orders/",
        json=_variant_order_body(
            product.id, red.id, quantity=1, shipping_method_id=test_shipping_method_id
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 422
    assert f"Variant {red.id} is unavailable" in resp.json()["detail"]


async def test_customer_cancel_restores_variant_stock(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=0, price_usd=20.0)
    red, _blue = _add_variants(db_session, product)

    placed = await client.post(
        "/api/v1/orders/",
        json=_variant_order_body(
            product.id, red.id, quantity=3, shipping_method_id=test_shipping_method_id
        ),
        headers=_headers(user),
    )
    assert placed.status_code == 201
    order_id = placed.json()["id"]
    db_session.refresh(red)
    assert red.stock == 1

    resp = await client.post(f"/api/v1/orders/{order_id}/cancel", headers=_headers(user))
    assert resp.status_code == 200
    db_session.refresh(red)
    db_session.refresh(product)
    assert red.stock == 4
    assert product.stock == 10
