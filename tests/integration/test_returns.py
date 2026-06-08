"""Integration tests for the returns (RMA) routes.

Covers:

- ``GET /api/v1/orders/{id}/returns/eligibility`` — owner-scoped (404 for
  others), reason codes (not delivered / disabled / window closed), per-line
  returnable quantities.
- ``POST /api/v1/orders/{id}/returns`` — 201 happy path (+ "requested" email
  scheduled), 400 on ineligibility / over-quantity, 404 not-owned.
- ``GET /api/v1/orders/{id}/returns`` — scoped list.
- Admin ``/api/v1/admin/returns``: gating (401/403), paginated list + status
  filter, detail, approve/reject/receive guarded transitions (+ emails +
  audit), and ``POST /{id}/refund`` delegating to the existing refund
  machinery (refund row + KOR + restock movement + order status flip + return
  flip with ``refund_id``/``resolved_at``; allowed from received AND approved;
  one-refund-per-return).
- Variant coordination: restock-from-return moves the variant's stock.
- The admin order PATCH delivered_at side effect (transition into delivered).

Plan: ``.claude/notes/plans/shopify-gap/pending/2026-06-07-guest-discounts-rma.md``
Feature C.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.inventory_movement import InventoryMovement
from app.models.order import Order
from app.models.refund import Refund
from app.models.user import UserRole
from app.services import order_email, return_email

pytestmark = pytest.mark.integration

ADMIN_RETURNS = "/api/v1/admin/returns"


@pytest.fixture
def scheduled_return_emails(monkeypatch) -> list[tuple[int, str]]:
    """Capture ``(return_id, event)`` the routes schedule.

    The real ``send_return_event_in_background`` opens its own DB session that
    can't see the test's savepoint-isolated rows — stub it to a list-recording
    fake (mirrors the fixture in ``test_refunds.py``). Email *composition* is
    covered separately below by calling ``send_return_event`` directly.
    """
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        return_email,
        "send_return_event_in_background",
        lambda return_id, event: calls.append((return_id, event)),
    )
    return calls


@pytest.fixture
def scheduled_order_emails(monkeypatch) -> list[tuple[int, str]]:
    """Capture ``(order_id, event)`` order emails (the refund route fires the
    existing order "refunded" event)."""
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


async def _place_and_deliver_order(
    client,
    admin,
    customer,
    product,
    *,
    shipping_method_id: int,
    quantity: int = 2,
    variant_id: int | None = None,
) -> dict:
    """Create a pending order as the customer and walk it to delivered via the
    admin PATCH workflow (pending → paid → shipped → delivered). Returns the
    delivered order's JSON body."""
    body = _order_body(product.id, shipping_method_id=shipping_method_id, quantity=quantity)
    if variant_id is not None:
        body["items"][0]["variant_id"] = variant_id
    placed = await client.post("/api/v1/orders/", json=body, headers=_headers(customer))
    assert placed.status_code == 201, placed.text
    order_id = placed.json()["id"]
    delivered: dict = {}
    for target in ("paid", "shipped", "delivered"):
        resp = await client.patch(
            f"/api/v1/orders/{order_id}",
            json={"status": target},
            headers=_headers(admin),
        )
        assert resp.status_code == 200, resp.text
        delivered = resp.json()
    return delivered


def _return_body(order: dict, *, quantity: int = 1, reason: str = "defective") -> dict:
    return {
        "items": [
            {
                "order_item_id": order["items"][0]["id"],
                "quantity": quantity,
                "reason": reason,
            }
        ],
        "comment": "Arrived broken",
    }


# ----------------------------------------------------------------------
# Admin order PATCH — delivered_at side effect (Touched-BE item)
# ----------------------------------------------------------------------


async def test_admin_patch_into_delivered_stamps_delivered_at(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="delat-cust@example.com")
    admin = user_factory(email="delat-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)

    body = _order_body(product.id, shipping_method_id=test_shipping_method_id)
    placed = await client.post("/api/v1/orders/", json=body, headers=_headers(customer))
    assert placed.status_code == 201
    assert placed.json()["delivered_at"] is None
    order_id = placed.json()["id"]

    for target in ("paid", "shipped"):
        resp = await client.patch(
            f"/api/v1/orders/{order_id}", json={"status": target}, headers=_headers(admin)
        )
        assert resp.status_code == 200
        assert resp.json()["delivered_at"] is None

    delivered = await client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "delivered"}, headers=_headers(admin)
    )
    assert delivered.status_code == 200
    stamped = delivered.json()["delivered_at"]
    assert stamped is not None

    # A no-op delivered→delivered PATCH must not move the timestamp.
    again = await client.patch(
        f"/api/v1/orders/{order_id}",
        json={"status": "delivered", "notes": "checked"},
        headers=_headers(admin),
    )
    assert again.status_code == 200
    assert again.json()["delivered_at"] == stamped


# ----------------------------------------------------------------------
# Eligibility endpoint
# ----------------------------------------------------------------------


async def test_eligibility_requires_auth_and_ownership(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="elig-cust1@example.com")
    other = user_factory(email="elig-other1@example.com")
    admin = user_factory(email="elig-admin1@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    order = await _place_and_deliver_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )

    assert (await client.get(f"/api/v1/orders/{order['id']}/returns/eligibility")).status_code == (
        401
    )
    # Not-owned → 404 (existence not leaked).
    resp = await client.get(
        f"/api/v1/orders/{order['id']}/returns/eligibility", headers=_headers(other)
    )
    assert resp.status_code == 404
    # Admin sees any.
    resp = await client.get(
        f"/api/v1/orders/{order['id']}/returns/eligibility", headers=_headers(admin)
    )
    assert resp.status_code == 200


async def test_eligibility_not_delivered(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="elig-cust2@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    placed = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=test_shipping_method_id),
        headers=_headers(customer),
    )
    assert placed.status_code == 201

    resp = await client.get(
        f"/api/v1/orders/{placed.json()['id']}/returns/eligibility", headers=_headers(customer)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["eligible"] is False
    assert body["reason"] == "not_delivered"
    assert body["window_ends_at"] is None


async def test_eligibility_delivered_order_lines_and_window(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="elig-cust3@example.com")
    admin = user_factory(email="elig-admin3@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=12.5)
    order = await _place_and_deliver_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id, quantity=2
    )

    resp = await client.get(
        f"/api/v1/orders/{order['id']}/returns/eligibility", headers=_headers(customer)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["eligible"] is True
    assert body["reason"] is None
    assert body["window_ends_at"] is not None
    assert body["lines"] == [
        {
            "order_item_id": order["items"][0]["id"],
            "product_name": product.name,
            "variant_title": None,
            "quantity": 2,
            "returnable_qty": 2,
        }
    ]


async def test_eligibility_window_closed(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    customer = user_factory(email="elig-cust4@example.com")
    admin = user_factory(email="elig-admin4@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    order = await _place_and_deliver_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )
    # Backdate the delivery far past the 30-day default window.
    db_session.query(Order).filter(Order.id == order["id"]).update(
        {"delivered_at": datetime.now(UTC).replace(tzinfo=None) - timedelta(days=90)}
    )
    db_session.flush()

    resp = await client.get(
        f"/api/v1/orders/{order['id']}/returns/eligibility", headers=_headers(customer)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["eligible"] is False
    assert body["reason"] == "window_closed"

    create = await client.post(
        f"/api/v1/orders/{order['id']}/returns",
        json=_return_body(order),
        headers=_headers(customer),
    )
    assert create.status_code == 400
    assert "window" in create.json()["detail"].lower()


async def test_eligibility_returns_disabled(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="elig-cust5@example.com")
    admin = user_factory(email="elig-admin5@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    order = await _place_and_deliver_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )
    resp = await client.patch(
        "/api/v1/admin/settings", json={"returns_window_days": 0}, headers=_headers(admin)
    )
    assert resp.status_code == 200

    elig = await client.get(
        f"/api/v1/orders/{order['id']}/returns/eligibility", headers=_headers(customer)
    )
    assert elig.status_code == 200
    assert elig.json()["eligible"] is False
    assert elig.json()["reason"] == "returns_disabled"

    create = await client.post(
        f"/api/v1/orders/{order['id']}/returns",
        json=_return_body(order),
        headers=_headers(customer),
    )
    assert create.status_code == 400
    assert "disabled" in create.json()["detail"].lower()


# ----------------------------------------------------------------------
# POST /orders/{id}/returns — create
# ----------------------------------------------------------------------


async def test_create_return_happy_path(
    client,
    user_factory,
    product_factory,
    test_shipping_method_id,
    scheduled_return_emails,
):
    customer = user_factory(email="ret-cust1@example.com")
    admin = user_factory(email="ret-admin1@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=12.5)
    order = await _place_and_deliver_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id, quantity=2
    )

    resp = await client.post(
        f"/api/v1/orders/{order['id']}/returns",
        json=_return_body(order, quantity=2),
        headers=_headers(customer),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["order_id"] == order["id"]
    assert body["user_id"] == customer.id
    assert body["status"] == "requested"
    assert body["comment"] == "Arrived broken"
    assert body["refund_id"] is None
    assert body["resolved_at"] is None
    assert body["order_number"] == order["order_number"]
    assert body["currency"] == "USD"
    assert len(body["items"]) == 1
    assert body["items"][0]["quantity"] == 2
    assert body["items"][0]["reason"] == "defective"
    assert body["items"][0]["product_name"] == product.name
    assert body["items"][0]["line_subtotal"] == pytest.approx(25.0)

    # "requested" email scheduled for this return.
    assert (body["id"], "requested") in scheduled_return_emails


async def test_create_return_rejects_over_quantity_and_cross_customer(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="ret-cust2@example.com")
    other = user_factory(email="ret-other2@example.com")
    admin = user_factory(email="ret-admin2@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    order = await _place_and_deliver_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id, quantity=2
    )

    # Over-ask → 400 with the remaining quantity in the message.
    resp = await client.post(
        f"/api/v1/orders/{order['id']}/returns",
        json=_return_body(order, quantity=3),
        headers=_headers(customer),
    )
    assert resp.status_code == 400
    assert "returnable" in resp.json()["detail"]

    # Someone else's order → 404 (no existence leak).
    resp = await client.post(
        f"/api/v1/orders/{order['id']}/returns",
        json=_return_body(order),
        headers=_headers(other),
    )
    assert resp.status_code == 404


async def test_create_second_return_caps_at_remaining_quantity(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="ret-cust3@example.com")
    admin = user_factory(email="ret-admin3@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    order = await _place_and_deliver_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id, quantity=3
    )

    first = await client.post(
        f"/api/v1/orders/{order['id']}/returns",
        json=_return_body(order, quantity=2),
        headers=_headers(customer),
    )
    assert first.status_code == 201

    # 2 of 3 already pending → only 1 left.
    over = await client.post(
        f"/api/v1/orders/{order['id']}/returns",
        json=_return_body(order, quantity=2),
        headers=_headers(customer),
    )
    assert over.status_code == 400

    ok = await client.post(
        f"/api/v1/orders/{order['id']}/returns",
        json=_return_body(order, quantity=1, reason="no_longer_needed"),
        headers=_headers(customer),
    )
    assert ok.status_code == 201


# ----------------------------------------------------------------------
# GET /orders/{id}/returns — scoped list
# ----------------------------------------------------------------------


async def test_list_order_returns_scoped(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="ret-cust4@example.com")
    other = user_factory(email="ret-other4@example.com")
    admin = user_factory(email="ret-admin4@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    order = await _place_and_deliver_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )
    created = await client.post(
        f"/api/v1/orders/{order['id']}/returns",
        json=_return_body(order),
        headers=_headers(customer),
    )
    assert created.status_code == 201

    mine = await client.get(f"/api/v1/orders/{order['id']}/returns", headers=_headers(customer))
    assert mine.status_code == 200
    assert len(mine.json()) == 1
    assert mine.json()[0]["status"] == "requested"

    assert (
        await client.get(f"/api/v1/orders/{order['id']}/returns", headers=_headers(other))
    ).status_code == 404

    admin_view = await client.get(f"/api/v1/orders/{order['id']}/returns", headers=_headers(admin))
    assert admin_view.status_code == 200
    assert len(admin_view.json()) == 1


# ----------------------------------------------------------------------
# Admin gating + list + detail
# ----------------------------------------------------------------------


async def test_admin_returns_endpoints_are_admin_gated(client, user_factory):
    customer = user_factory(email="gate-cust@example.com")
    assert (await client.get(f"{ADMIN_RETURNS}/")).status_code == 401
    assert (await client.get(f"{ADMIN_RETURNS}/", headers=_headers(customer))).status_code == 403
    assert (await client.get(f"{ADMIN_RETURNS}/1", headers=_headers(customer))).status_code == 403
    for action in ("approve", "reject", "receive", "refund"):
        resp = await client.post(f"{ADMIN_RETURNS}/1/{action}", headers=_headers(customer))
        assert resp.status_code == 403, action


async def test_admin_list_pagination_and_status_filter(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="adm-cust1@example.com")
    admin = user_factory(email="adm-admin1@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=20, price_usd=10.0)
    order = await _place_and_deliver_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id, quantity=4
    )
    first = (
        await client.post(
            f"/api/v1/orders/{order['id']}/returns",
            json=_return_body(order, quantity=1),
            headers=_headers(customer),
        )
    ).json()
    second = (
        await client.post(
            f"/api/v1/orders/{order['id']}/returns",
            json=_return_body(order, quantity=1, reason="other"),
            headers=_headers(customer),
        )
    ).json()
    approved = await client.post(f"{ADMIN_RETURNS}/{first['id']}/approve", headers=_headers(admin))
    assert approved.status_code == 200

    resp = await client.get(f"{ADMIN_RETURNS}/", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    for key in ("items", "total", "page", "page_size", "pages"):
        assert key in body
    assert body["total"] >= 2

    filtered = await client.get(
        f"{ADMIN_RETURNS}/", params={"status": "approved"}, headers=_headers(admin)
    )
    assert filtered.status_code == 200
    ids = [item["id"] for item in filtered.json()["items"]]
    assert first["id"] in ids
    assert second["id"] not in ids

    detail = await client.get(f"{ADMIN_RETURNS}/{second['id']}", headers=_headers(admin))
    assert detail.status_code == 200
    assert detail.json()["status"] == "requested"
    assert detail.json()["order_number"] == order["order_number"]
    assert detail.json()["customer_email"] == "adm-cust1@example.com"

    assert (await client.get(f"{ADMIN_RETURNS}/999999", headers=_headers(admin))).status_code == (
        404
    )


# ----------------------------------------------------------------------
# Admin transitions (approve / reject / receive) + audit + emails
# ----------------------------------------------------------------------


async def _create_requested_return(
    client, user_factory, product_factory, test_shipping_method_id, *, tag: str, quantity: int = 2
):
    customer = user_factory(email=f"tr-cust-{tag}@example.com")
    admin = user_factory(email=f"tr-admin-{tag}@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    order = await _place_and_deliver_order(
        client,
        admin,
        customer,
        product,
        shipping_method_id=test_shipping_method_id,
        quantity=quantity,
    )
    created = await client.post(
        f"/api/v1/orders/{order['id']}/returns",
        json=_return_body(order, quantity=quantity),
        headers=_headers(customer),
    )
    assert created.status_code == 201
    return admin, customer, product, order, created.json()


def _return_audit_count(db: Session, action: str) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(
            AdminAuditEvent.resource_type == "return_request",
            AdminAuditEvent.action == action,
        )
        .count()
    )


async def test_approve_then_receive_flow_with_audit_and_email(
    client,
    user_factory,
    product_factory,
    db_session: Session,
    test_shipping_method_id,
    scheduled_return_emails,
):
    admin, _, _, _, ret = await _create_requested_return(
        client, user_factory, product_factory, test_shipping_method_id, tag="appr"
    )
    before = _return_audit_count(db_session, "update")

    approved = await client.post(
        f"{ADMIN_RETURNS}/{ret['id']}/approve",
        json={"admin_note": "ok to return"},
        headers=_headers(admin),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["admin_note"] == "ok to return"
    assert approved.json()["resolved_at"] is None
    assert (ret["id"], "approved") in scheduled_return_emails
    assert _return_audit_count(db_session, "update") == before + 1

    received = await client.post(f"{ADMIN_RETURNS}/{ret['id']}/receive", headers=_headers(admin))
    assert received.status_code == 200
    assert received.json()["status"] == "received"
    # receive does NOT restock and does not resolve.
    assert received.json()["resolved_at"] is None
    assert _return_audit_count(db_session, "update") == before + 2


async def test_reject_is_terminal_and_emails(
    client,
    user_factory,
    product_factory,
    db_session: Session,
    test_shipping_method_id,
    scheduled_return_emails,
):
    admin, customer, _, order, ret = await _create_requested_return(
        client, user_factory, product_factory, test_shipping_method_id, tag="rej"
    )

    rejected = await client.post(
        f"{ADMIN_RETURNS}/{ret['id']}/reject",
        json={"admin_note": "outside policy"},
        headers=_headers(admin),
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["resolved_at"] is not None
    assert (ret["id"], "rejected") in scheduled_return_emails

    # Terminal: further transitions 400.
    again = await client.post(f"{ADMIN_RETURNS}/{ret['id']}/approve", headers=_headers(admin))
    assert again.status_code == 400

    # A rejected return gives the quantity back — the customer can re-request.
    elig = await client.get(
        f"/api/v1/orders/{order['id']}/returns/eligibility", headers=_headers(customer)
    )
    assert elig.json()["eligible"] is True
    assert elig.json()["lines"][0]["returnable_qty"] == 2


async def test_illegal_transitions_400(
    client, user_factory, product_factory, test_shipping_method_id
):
    admin, _, _, _, ret = await _create_requested_return(
        client, user_factory, product_factory, test_shipping_method_id, tag="illegal"
    )

    # requested → received is not allowed (must approve first).
    resp = await client.post(f"{ADMIN_RETURNS}/{ret['id']}/receive", headers=_headers(admin))
    assert resp.status_code == 400
    assert "transition" in resp.json()["detail"].lower()


# ----------------------------------------------------------------------
# Refund-from-return
# ----------------------------------------------------------------------


async def test_refund_from_received_full_flow(
    client,
    user_factory,
    product_factory,
    db_session: Session,
    test_shipping_method_id,
    scheduled_order_emails,
):
    """received → refund: refund row + KOR + restock movement + order status
    flip + return flip (refund_id, resolved_at) + order 'refunded' email."""
    admin, _customer, product, order, ret = await _create_requested_return(
        client, user_factory, product_factory, test_shipping_method_id, tag="refund1"
    )
    for action in ("approve", "receive"):
        resp = await client.post(f"{ADMIN_RETURNS}/{ret['id']}/{action}", headers=_headers(admin))
        assert resp.status_code == 200
    scheduled_order_emails.clear()

    before = _return_audit_count(db_session, "update")
    resp = await client.post(
        f"{ADMIN_RETURNS}/{ret['id']}/refund",
        json={"restock": True},
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "refunded"
    assert body["refund_id"] is not None
    assert body["resolved_at"] is not None
    assert _return_audit_count(db_session, "update") == before + 1

    # The refund row: amount defaulted to the returned lines' subtotal
    # (2 x 10.0, $0 test shipping → equals order total) in the order currency.
    refund = db_session.get(Refund, body["refund_id"])
    assert refund is not None
    assert refund.amount == pytest.approx(20.0)
    assert refund.currency == "USD"
    assert refund.reason == "customer_request"
    assert refund.restock is True
    # KOR issued (the paid-via-admin-PATCH path gave the order a faktura).
    assert refund.corrective_invoice is not None

    # Order flipped (full amount → refunded) and the restock movement exists.
    order_detail = await client.get(f"/api/v1/orders/{order['id']}", headers=_headers(admin))
    assert order_detail.json()["status"] == "refunded"
    movement = (
        db_session.query(InventoryMovement)
        .filter(
            InventoryMovement.order_id == order["id"],
            InventoryMovement.reason == "order_refunded",
        )
        .one()
    )
    assert movement.delta == 2
    db_session.refresh(product)
    assert product.stock == 10  # back to the factory's seed

    # The existing order "refunded" email (KOR attached) was scheduled.
    assert (order["id"], "refunded") in scheduled_order_emails

    # One refund per return: a second refund 400s (status already refunded).
    again = await client.post(
        f"{ADMIN_RETURNS}/{ret['id']}/refund", json={"restock": False}, headers=_headers(admin)
    )
    assert again.status_code == 400


async def test_refund_allowed_from_approved(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    """The ship-back-not-required flow: refund directly from ``approved``."""
    admin, _, _, _, ret = await _create_requested_return(
        client, user_factory, product_factory, test_shipping_method_id, tag="refund2"
    )
    resp = await client.post(f"{ADMIN_RETURNS}/{ret['id']}/approve", headers=_headers(admin))
    assert resp.status_code == 200

    refunded = await client.post(
        f"{ADMIN_RETURNS}/{ret['id']}/refund",
        json={"restock": False, "reason": "defective", "note": "no ship-back"},
        headers=_headers(admin),
    )
    assert refunded.status_code == 200, refunded.text
    assert refunded.json()["status"] == "refunded"
    refund = db_session.get(Refund, refunded.json()["refund_id"])
    assert refund is not None
    assert refund.reason == "defective"
    assert refund.note == "no ship-back"
    assert refund.restock is False


async def test_refund_rejected_from_requested_status(
    client, user_factory, product_factory, test_shipping_method_id
):
    admin, _, _, _, ret = await _create_requested_return(
        client, user_factory, product_factory, test_shipping_method_id, tag="refund3"
    )
    resp = await client.post(
        f"{ADMIN_RETURNS}/{ret['id']}/refund", json={"restock": False}, headers=_headers(admin)
    )
    assert resp.status_code == 400
    assert "received, approved" in resp.json()["detail"]


async def test_refund_custom_partial_amount(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    """An explicit partial ``amount`` (e.g. minus a restocking fee) flows
    through to the refund row and leaves the order partially refunded."""
    admin, _, _, order, ret = await _create_requested_return(
        client, user_factory, product_factory, test_shipping_method_id, tag="refund4"
    )
    for action in ("approve", "receive"):
        await client.post(f"{ADMIN_RETURNS}/{ret['id']}/{action}", headers=_headers(admin))

    resp = await client.post(
        f"{ADMIN_RETURNS}/{ret['id']}/refund",
        json={"amount": 15.0, "restock": False},
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    refund = db_session.get(Refund, resp.json()["refund_id"])
    assert refund is not None
    assert refund.amount == pytest.approx(15.0)
    order_detail = await client.get(f"/api/v1/orders/{order['id']}", headers=_headers(admin))
    assert order_detail.json()["status"] == "partially_refunded"


# ----------------------------------------------------------------------
# Variant coordination — restock-from-return moves the variant's stock
# ----------------------------------------------------------------------


async def test_refund_from_return_restocks_at_variant_grain(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    """The plan's coordination test: a returned variant line restocks at
    variant grain through the existing ``create_for_order`` apply_delta loop
    (variants program PR #150) with zero RMA-side changes."""
    from app.crud import product_variant as crud_product_variant
    from app.schemas.product_variant import ProductOptionsMatrixUpdate

    customer = user_factory(email="varret-cust@example.com")
    admin = user_factory(email="varret-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=0, price_usd=12.5)
    matrix = ProductOptionsMatrixUpdate.model_validate(
        {
            "options": [
                {"name": "Color", "position": 0, "values": [{"value": "Red", "position": 0}]}
            ],
            "variants": [
                {
                    "option_values": ["Red"],
                    "sku": f"RET-RED-{product.id}",
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

    order = await _place_and_deliver_order(
        client,
        admin,
        customer,
        product,
        shipping_method_id=test_shipping_method_id,
        quantity=2,
        variant_id=red.id,
    )
    db_session.refresh(red)
    assert red.stock == 3

    created = await client.post(
        f"/api/v1/orders/{order['id']}/returns",
        json=_return_body(order, quantity=2),
        headers=_headers(customer),
    )
    assert created.status_code == 201, created.text
    ret = created.json()
    assert ret["items"][0]["variant_title"] == "Red"

    for action in ("approve", "receive"):
        resp = await client.post(f"{ADMIN_RETURNS}/{ret['id']}/{action}", headers=_headers(admin))
        assert resp.status_code == 200

    refunded = await client.post(
        f"{ADMIN_RETURNS}/{ret['id']}/refund", json={"restock": True}, headers=_headers(admin)
    )
    assert refunded.status_code == 200, refunded.text

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


# ----------------------------------------------------------------------
# Email composition (EN/PL) — direct sends through the captured-email mock
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("locale", "needle"),
    [("en", "Return request received"), ("pl", "Otrzymano zgłoszenie zwrotu")],
)
async def test_requested_email_localized(
    client,
    user_factory,
    product_factory,
    db_session: Session,
    test_shipping_method_id,
    captured_emails,
    locale,
    needle,
):
    customer = user_factory(email=f"mail-cust-{locale}@example.com", locale=locale)
    admin = user_factory(email=f"mail-admin-{locale}@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    order = await _place_and_deliver_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )
    created = await client.post(
        f"/api/v1/orders/{order['id']}/returns",
        json=_return_body(order),
        headers=_headers(customer),
    )
    assert created.status_code == 201

    from app.crud import return_request as crud_return_request

    ret = crud_return_request.get(db_session, return_id=created.json()["id"])
    assert ret is not None
    captured_emails.clear()
    return_email.send_return_event(db_session, ret, "requested")

    assert len(captured_emails) == 1
    sent = captured_emails[0]
    assert sent["email_to"] == customer.email
    assert needle in sent["subject"]
    assert order["order_number"] in sent["subject"]


@pytest.mark.parametrize("event", ["approved", "rejected"])
async def test_decision_emails_compose(
    client,
    user_factory,
    product_factory,
    db_session: Session,
    test_shipping_method_id,
    captured_emails,
    event,
):
    customer = user_factory(email=f"mail2-cust-{event}@example.com", locale="en")
    admin = user_factory(email=f"mail2-admin-{event}@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10.0)
    order = await _place_and_deliver_order(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )
    created = await client.post(
        f"/api/v1/orders/{order['id']}/returns",
        json=_return_body(order),
        headers=_headers(customer),
    )
    assert created.status_code == 201

    from app.crud import return_request as crud_return_request

    ret = crud_return_request.get(db_session, return_id=created.json()["id"])
    assert ret is not None
    captured_emails.clear()
    return_email.send_return_event(db_session, ret, event)

    assert len(captured_emails) == 1
    assert captured_emails[0]["email_to"] == customer.email
    assert order["order_number"] in captured_emails[0]["subject"]
