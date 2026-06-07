"""Integration tests for the refund routes.

Covers:
- ``POST /api/v1/orders/{id}/refunds`` (admin-gated) — 201 happy path with
  the response embedding the actor email + KOR summary; 400 on partial /
  not-refundable; 403 for a non-admin; 404 for missing order.
- ``GET /api/v1/orders/{id}/refunds`` — scoped (customer sees own, admin sees
  any, cross-customer = 404).
- ``GET /api/v1/orders/{id}`` carries the new ``refunds`` array after a refund.
- Audit row is recorded (``resource_type="refund"`` ``action="create"``).
- Email background task is scheduled with ``event="refunded"`` and the KOR
  PDF is attached.

The refund plan: ``ecommerce_store/.claude/notes/plans/orders/pending/
2026-05-30-refunds-workflow-handoff.md``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.inventory_movement import InventoryMovement
from app.models.user import UserRole
from app.services import order_email

pytestmark = pytest.mark.integration


@pytest.fixture
def scheduled_emails(monkeypatch) -> list[tuple[int, str]]:
    """Capture ``(order_id, event)`` the routes schedule.

    Mirrors the fixture in ``tests/integration/test_order_emails.py``: the real
    ``send_order_event_in_background`` opens its own DB session that can't see
    the test's savepoint-isolated rows, so we stub it to a list-recording fake.
    The email *composition* (invoice + KOR attachment) is covered in
    ``tests/unit/test_order_email.py``.
    """
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        order_email,
        "send_order_event_in_background",
        lambda order_id, event: calls.append((order_id, event)),
    )
    return calls


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _order_body(product_id: int, *, shipping_method_id: int, quantity: int = 1) -> dict:
    return {
        "items": [{"product_id": product_id, "quantity": quantity}],
        "currency": "USD",
        "shipping_address": "1 Test St",
        "shipping_city": "Testville",
        "shipping_postal_code": "00-001",
        "shipping_country": "PL",
        "shipping_method_id": shipping_method_id,
    }


async def _place_and_pay_order(
    client, admin, customer, product, *, shipping_method_id: int, quantity: int = 1
) -> dict:
    """Create a pending order as the customer, flip it paid via admin PATCH.

    Returns the paid order's JSON body (incl. ``id`` + ``total`` + ``order_number``).
    The admin PATCH path triggers ``crud_invoice.issue_for_order`` so the order
    has a faktura — which is the precondition for a KOR being issued on refund.
    """
    placed = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=shipping_method_id, quantity=quantity),
        headers=_headers(customer),
    )
    assert placed.status_code == 201, placed.text
    order_id = placed.json()["id"]
    paid = await client.patch(
        f"/api/v1/orders/{order_id}",
        json={"status": "paid", "payment_method": "stripe"},
        headers=_headers(admin),
    )
    assert paid.status_code == 200, paid.text
    return paid.json()


# ----------------------------------------------------------------------
# POST /refunds — happy path + audit + email + restock
# ----------------------------------------------------------------------


async def test_admin_issue_refund_happy_path(
    client,
    user_factory,
    product_factory,
    db_session: Session,
    scheduled_emails,
    test_shipping_method_id,
):
    customer = user_factory(email="refund-cust@example.com")
    admin = user_factory(email="refund-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=12.50)
    paid = await _place_and_pay_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id, quantity=2
    )
    # Drop the (paid, …) events the place/pay flow scheduled — we only want
    # to assert on what the refund POST scheduled.
    scheduled_emails.clear()

    order_line_id = paid["items"][0]["id"]
    resp = await client.post(
        f"/api/v1/orders/{paid['id']}/refunds",
        json={
            "amount": paid["total"],
            "reason": "customer_request",
            "note": "Refunded per customer request.",
            "restock": True,
            "items": [{"order_item_id": order_line_id, "quantity": 2}],
        },
        headers=_headers(admin),
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["order_id"] == paid["id"]
    assert body["amount"] == pytest.approx(paid["total"])
    assert body["currency"] == "USD"
    assert body["reason"] == "customer_request"
    assert body["note"] == "Refunded per customer request."
    assert body["restock"] is True
    assert body["actor_email"] == "refund-admin@example.com"
    assert body["corrective_invoice"] is not None
    assert body["corrective_invoice"]["kor_number"].startswith(("KOR/", "KOR-"))
    assert len(body["items"]) == 1
    assert body["items"][0]["quantity"] == 2

    # Order status flipped on the GET (refunds carried in the detail response).
    detail = await client.get(f"/api/v1/orders/{paid['id']}", headers=_headers(admin))
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["status"] == "refunded"
    assert len(detail_body["refunds"]) == 1
    assert detail_body["refunds"][0]["actor_email"] == "refund-admin@example.com"

    # Audit row recorded as a "create" on resource_type "refund".
    audit = (
        db_session.query(AdminAuditEvent)
        .filter(
            AdminAuditEvent.resource_type == "refund",
            AdminAuditEvent.action == "create",
        )
        .count()
    )
    assert audit == 1

    # Restock wrote one order_refunded movement per line (here: one line).
    movements = (
        db_session.query(InventoryMovement)
        .filter(InventoryMovement.product_id == product.id)
        .filter(InventoryMovement.reason == "order_refunded")
        .all()
    )
    assert len(movements) == 1
    assert movements[0].delta == 2

    # The route scheduled the refunded email (composition + KOR attachment is
    # covered by the unit tests in tests/unit/test_order_email.py).
    assert (paid["id"], "refunded") in scheduled_emails


async def test_admin_issue_refund_restock_false_skips_movements(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    customer = user_factory(email="norestock-cust@example.com")
    admin = user_factory(email="norestock-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    paid = await _place_and_pay_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/orders/{paid['id']}/refunds",
        json={
            "amount": paid["total"],
            "reason": "lost_in_transit",
            "restock": False,
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 201

    refund_movements = (
        db_session.query(InventoryMovement)
        .filter(InventoryMovement.product_id == product.id)
        .filter(InventoryMovement.reason == "order_refunded")
        .all()
    )
    assert refund_movements == []


# ----------------------------------------------------------------------
# POST /refunds — error paths
# ----------------------------------------------------------------------


async def test_admin_issue_partial_refund_flips_partially_refunded(
    client, user_factory, product_factory, test_shipping_method_id
):
    """v2: partial amount is accepted; status flips to ``partially_refunded``."""
    customer = user_factory(email="partial-cust@example.com")
    admin = user_factory(email="partial-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    paid = await _place_and_pay_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/orders/{paid['id']}/refunds",
        json={"amount": paid["total"] / 2, "reason": "customer_request", "restock": False},
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text

    detail = await client.get(f"/api/v1/orders/{paid['id']}", headers=_headers(admin))
    assert detail.json()["status"] == "partially_refunded"


async def test_admin_issue_over_refund_400(
    client, user_factory, product_factory, test_shipping_method_id
):
    """v2: ``amount`` > ``order.total`` (running-total) → 400 ``RefundOverError``."""
    customer = user_factory(email="over-cust@example.com")
    admin = user_factory(email="over-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    paid = await _place_and_pay_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/orders/{paid['id']}/refunds",
        json={"amount": paid["total"] + 1, "reason": "other", "restock": False},
        headers=_headers(admin),
    )
    assert resp.status_code == 400
    assert "exceed" in resp.json()["detail"]


async def test_admin_two_partials_flip_to_refunded(
    client, user_factory, product_factory, test_shipping_method_id, scheduled_emails
):
    """v2: two consecutive partials whose sum == ``order.total`` flip status to ``refunded``."""
    customer = user_factory(email="two-cust@example.com")
    admin = user_factory(email="two-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=12.0)
    paid = await _place_and_pay_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id, quantity=2
    )
    total = paid["total"]

    first = await client.post(
        f"/api/v1/orders/{paid['id']}/refunds",
        json={"amount": total / 2, "reason": "customer_request", "restock": False},
        headers=_headers(admin),
    )
    assert first.status_code == 201

    after_first = await client.get(f"/api/v1/orders/{paid['id']}", headers=_headers(admin))
    assert after_first.json()["status"] == "partially_refunded"

    second = await client.post(
        f"/api/v1/orders/{paid['id']}/refunds",
        json={"amount": total / 2, "reason": "defective", "restock": False},
        headers=_headers(admin),
    )
    assert second.status_code == 201

    after_second = await client.get(f"/api/v1/orders/{paid['id']}", headers=_headers(admin))
    assert after_second.json()["status"] == "refunded"
    assert len(after_second.json()["refunds"]) == 2


async def test_admin_issue_refund_line_picker_persists(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    """v2: picked ``items`` round-trip on the response with denormalised name + price."""
    customer = user_factory(email="picker-cust@example.com")
    admin = user_factory(email="picker-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=15.0, name="Widget X")
    paid = await _place_and_pay_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id, quantity=3
    )
    order_line_id = paid["items"][0]["id"]

    resp = await client.post(
        f"/api/v1/orders/{paid['id']}/refunds",
        json={
            "amount": 30.0,  # 2 x 15
            "reason": "defective",
            "restock": True,
            "items": [{"order_item_id": order_line_id, "quantity": 2}],
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["items"]) == 1
    line = body["items"][0]
    assert line["order_item_id"] == order_line_id
    assert line["quantity"] == 2
    assert line["product_name"] == "Widget X"
    assert line["unit_price"] == pytest.approx(15.0)
    assert line["line_subtotal"] == pytest.approx(30.0)


async def test_admin_issue_refund_restock_without_items_400(
    client, user_factory, product_factory, test_shipping_method_id
):
    """D5: ``restock=True`` without ``items`` → 400 ``RestockRequiresItemsError``."""
    customer = user_factory(email="d5-cust@example.com")
    admin = user_factory(email="d5-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    paid = await _place_and_pay_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/orders/{paid['id']}/refunds",
        json={"amount": paid["total"], "reason": "defective", "restock": True},
        headers=_headers(admin),
    )
    assert resp.status_code == 400
    assert "restock" in resp.json()["detail"].lower()


async def test_admin_issue_refund_line_over_qty_400(
    client, user_factory, product_factory, test_shipping_method_id
):
    """v2: picked qty > line.quantity → 400 ``RefundLineOverError``.

    Uses a smaller amount than ``order.total`` so the running-total guard
    doesn't fire first — the test asserts the line-qty guard in isolation.
    """
    customer = user_factory(email="lineover-cust@example.com")
    admin = user_factory(email="lineover-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    paid = await _place_and_pay_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id, quantity=2
    )
    order_line_id = paid["items"][0]["id"]

    resp = await client.post(
        f"/api/v1/orders/{paid['id']}/refunds",
        json={
            "amount": 1.0,  # well under any total — isolates the line guard
            "reason": "defective",
            "restock": False,
            "items": [{"order_item_id": order_line_id, "quantity": 5}],  # only 2 on the line
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 400
    assert "remain refundable" in resp.json()["detail"]


async def test_admin_patch_to_partially_refunded_points_at_endpoint(
    client, user_factory, product_factory, test_shipping_method_id
):
    """v2: admin PATCH ``status=partially_refunded`` 400s with the "use refund endpoint" hint."""
    customer = user_factory(email="patchpart-cust@example.com")
    admin = user_factory(email="patchpart-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    paid = await _place_and_pay_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.patch(
        f"/api/v1/orders/{paid['id']}",
        json={"status": "partially_refunded"},
        headers=_headers(admin),
    )
    assert resp.status_code == 400
    assert "refunds" in resp.json()["detail"]
    assert "POST" in resp.json()["detail"]


async def test_admin_issue_refund_on_pending_400(
    client, user_factory, product_factory, test_shipping_method_id
):
    """An order that hasn't transitioned to paid yet can't be refunded — D3 guard."""
    customer = user_factory(email="pending-cust@example.com")
    admin = user_factory(email="pending-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    placed = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=test_shipping_method_id),
        headers=_headers(customer),
    )
    order_id = placed.json()["id"]

    resp = await client.post(
        f"/api/v1/orders/{order_id}/refunds",
        json={"amount": placed.json()["total"], "reason": "other", "restock": False},
        headers=_headers(admin),
    )
    assert resp.status_code == 400
    assert "pending" in resp.json()["detail"]


async def test_admin_issue_refund_404_on_missing(client, user_factory):
    admin = user_factory(email="missing-admin@example.com", role=UserRole.ADMIN)
    resp = await client.post(
        "/api/v1/orders/999999/refunds",
        json={"amount": 10.0, "reason": "other", "restock": False},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


async def test_customer_cannot_issue_refund_403(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="cust-only@example.com")
    admin = user_factory(email="for-paid@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    paid = await _place_and_pay_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/orders/{paid['id']}/refunds",
        json={"amount": paid["total"], "reason": "other", "restock": False},
        headers=_headers(customer),
    )
    assert resp.status_code == 403


# ----------------------------------------------------------------------
# GET /refunds — scoped reads
# ----------------------------------------------------------------------


async def test_customer_can_list_own_order_refunds(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="ownlist-cust@example.com")
    admin = user_factory(email="ownlist-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    paid = await _place_and_pay_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )
    issued = await client.post(
        f"/api/v1/orders/{paid['id']}/refunds",
        json={"amount": paid["total"], "reason": "customer_request", "restock": False},
        headers=_headers(admin),
    )
    assert issued.status_code == 201

    resp = await client.get(f"/api/v1/orders/{paid['id']}/refunds", headers=_headers(customer))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["order_id"] == paid["id"]
    # The customer-side surface intentionally drops note client-side; the
    # endpoint still exposes it (admin uses the same shape).


async def test_other_customer_gets_404_on_refunds(
    client, user_factory, product_factory, test_shipping_method_id
):
    cust_a = user_factory(email="cust-a@example.com")
    cust_b = user_factory(email="cust-b@example.com")
    admin = user_factory(email="cross-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    paid = await _place_and_pay_order(
        client, admin, cust_a, product, shipping_method_id=test_shipping_method_id
    )
    await client.post(
        f"/api/v1/orders/{paid['id']}/refunds",
        json={"amount": paid["total"], "reason": "other", "restock": False},
        headers=_headers(admin),
    )

    resp = await client.get(f"/api/v1/orders/{paid['id']}/refunds", headers=_headers(cust_b))
    assert resp.status_code == 404


async def test_admin_lists_any_order_refunds(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="any-cust@example.com")
    admin = user_factory(email="any-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    paid = await _place_and_pay_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )
    order_line_id = paid["items"][0]["id"]
    create_resp = await client.post(
        f"/api/v1/orders/{paid['id']}/refunds",
        json={
            "amount": paid["total"],
            "reason": "defective",
            "restock": True,
            "items": [{"order_item_id": order_line_id, "quantity": 1}],
        },
        headers=_headers(admin),
    )
    assert create_resp.status_code == 201, create_resp.text

    resp = await client.get(f"/api/v1/orders/{paid['id']}/refunds", headers=_headers(admin))
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["actor_email"] == "any-admin@example.com"


# ----------------------------------------------------------------------
# Admin PATCH-to-refunded continues to be rejected (updated message)
# ----------------------------------------------------------------------


async def test_admin_patch_to_refunded_points_at_endpoint(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="patchref-cust@example.com")
    admin = user_factory(email="patchref-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    paid = await _place_and_pay_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.patch(
        f"/api/v1/orders/{paid['id']}",
        json={"status": "refunded"},
        headers=_headers(admin),
    )
    assert resp.status_code == 400
    # Updated message points the caller at the dedicated endpoint.
    assert "refunds" in resp.json()["detail"]
    assert "POST" in resp.json()["detail"]


# ----------------------------------------------------------------------
# Variant lines (PR-B2) — restock at variant grain
# ----------------------------------------------------------------------


async def test_refund_restock_at_variant_grain(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    """A picked variant line restocks at variant grain: the movement carries
    ``variant_id`` and variant.stock + product.stock move in lock-step."""
    from app.crud import product_variant as crud_product_variant
    from app.schemas.product_variant import ProductOptionsMatrixUpdate

    customer = user_factory(email="varrefund-cust@example.com")
    admin = user_factory(email="varrefund-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=0, price_usd=12.50)
    matrix = ProductOptionsMatrixUpdate.model_validate(
        {
            "options": [
                {
                    "name": "Color",
                    "position": 0,
                    "values": [{"value": "Red", "position": 0}],
                }
            ],
            "variants": [
                {
                    "option_values": ["Red"],
                    "sku": f"VR-RED-{product.id}",
                    "initial_stock": 5,
                    "position": 0,
                }
            ],
        }
    )
    crud_product_variant.apply_matrix(db_session, product=product, matrix=matrix)
    db_session.commit()
    db_session.refresh(product)
    red = crud_product_variant.get_for_product(db_session, product_id=product.id)[0]

    body = _order_body(product.id, shipping_method_id=test_shipping_method_id, quantity=2)
    body["items"][0]["variant_id"] = red.id
    placed = await client.post("/api/v1/orders/", json=body, headers=_headers(customer))
    assert placed.status_code == 201, placed.text
    order_id = placed.json()["id"]
    paid = await client.patch(
        f"/api/v1/orders/{order_id}",
        json={"status": "paid", "payment_method": "stripe"},
        headers=_headers(admin),
    )
    assert paid.status_code == 200, paid.text
    paid_body = paid.json()
    db_session.refresh(red)
    assert red.stock == 3

    resp = await client.post(
        f"/api/v1/orders/{order_id}/refunds",
        json={
            "amount": paid_body["total"],
            "reason": "customer_request",
            "restock": True,
            "items": [{"order_item_id": paid_body["items"][0]["id"], "quantity": 2}],
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text

    db_session.refresh(red)
    db_session.refresh(product)
    assert red.stock == 5
    assert product.stock == 5

    movement = (
        db_session.query(InventoryMovement)
        .filter(
            InventoryMovement.product_id == product.id,
            InventoryMovement.reason == "order_refunded",
        )
        .one()
    )
    assert movement.variant_id == red.id
    assert movement.delta == 2
