"""Integration tests for the per-order timeline (order_events, Back-office A3).

Covers the system events emitted at the order signal points (created / paid /
status_changed / tracking_added / refunded), the staff-comment POST + its
capability gate, the reverse-chron list endpoint, and authz. The `email_sent`
kind is intentionally not wired in v1 (background email dispatch runs in a
separate session/txn) — see the A3 plan.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.capabilities import CATALOG_WRITE, ORDERS_WRITE
from app.core.security import create_access_token
from app.models.order_event import OrderEvent
from app.models.user import UserRole
from app.services import order_email

pytestmark = pytest.mark.integration


@pytest.fixture
def scheduled_emails(monkeypatch) -> list[tuple[int, str]]:
    """Stub the background email dispatch (opens its own session in prod)."""
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


async def _place_order(client, customer, product, *, shipping_method_id: int) -> dict:
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=shipping_method_id),
        headers=_headers(customer),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _kinds(db: Session, order_id: int) -> list[str]:
    return [
        e.kind
        for e in db.query(OrderEvent)
        .filter(OrderEvent.order_id == order_id)
        .order_by(OrderEvent.id)
        .all()
    ]


# ---------------------------------------------------------------------------
# system events at the signal points
# ---------------------------------------------------------------------------


async def test_created_event_on_order_placement(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    customer = user_factory(email="oe-cust1@example.com")
    product = product_factory(stock=10, price_usd=10)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id
    )
    assert _kinds(db_session, order["id"]) == ["created"]


async def test_paid_event_on_admin_mark_paid(
    client,
    user_factory,
    product_factory,
    db_session: Session,
    test_shipping_method_id,
    scheduled_emails,
):
    customer = user_factory(email="oe-cust2@example.com")
    admin = user_factory(email="oe-admin2@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.patch(
        f"/api/v1/orders/{order['id']}",
        json={"status": "paid", "payment_method": "stripe"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    assert _kinds(db_session, order["id"]) == ["created", "paid"]


async def test_status_changed_and_tracking_events(
    client,
    user_factory,
    product_factory,
    db_session: Session,
    test_shipping_method_id,
    scheduled_emails,
):
    customer = user_factory(email="oe-cust3@example.com")
    admin = user_factory(email="oe-admin3@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id
    )
    oid = order["id"]

    await client.patch(
        f"/api/v1/orders/{oid}",
        json={"status": "paid", "payment_method": "stripe"},
        headers=_headers(admin),
    )
    # ship + tracking in one PATCH → both status_changed and tracking_added.
    resp = await client.patch(
        f"/api/v1/orders/{oid}",
        json={"status": "shipped", "tracking_number": "TRACK123", "shipping_carrier": "InPost"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    kinds = _kinds(db_session, oid)
    assert kinds == ["created", "paid", "status_changed", "tracking_added"]

    # status_changed payload records from/to.
    sc = (
        db_session.query(OrderEvent)
        .filter(OrderEvent.order_id == oid, OrderEvent.kind == "status_changed")
        .one()
    )
    assert sc.payload == {"from": "paid", "to": "shipped"}
    assert sc.actor_id == admin.id


async def test_status_changed_event_on_cancel(
    client,
    user_factory,
    product_factory,
    db_session: Session,
    test_shipping_method_id,
    scheduled_emails,
):
    customer = user_factory(email="oe-cust4@example.com")
    product = product_factory(stock=10, price_usd=10)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(f"/api/v1/orders/{order['id']}/cancel", headers=_headers(customer))
    assert resp.status_code == 200, resp.text
    kinds = _kinds(db_session, order["id"])
    assert kinds == ["created", "status_changed"]
    cancelled = (
        db_session.query(OrderEvent)
        .filter(OrderEvent.order_id == order["id"], OrderEvent.kind == "status_changed")
        .one()
    )
    assert cancelled.payload == {"from": "pending", "to": "cancelled"}


async def test_refunded_event_on_refund(
    client,
    user_factory,
    product_factory,
    db_session: Session,
    test_shipping_method_id,
    scheduled_emails,
):
    customer = user_factory(email="oe-cust5@example.com")
    admin = user_factory(email="oe-admin5@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id
    )
    paid = await client.patch(
        f"/api/v1/orders/{order['id']}",
        json={"status": "paid", "payment_method": "stripe"},
        headers=_headers(admin),
    )
    assert paid.status_code == 200, paid.text

    resp = await client.post(
        f"/api/v1/orders/{order['id']}/refunds",
        json={"amount": paid.json()["total"], "reason": "customer_request"},
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    assert _kinds(db_session, order["id"]) == ["created", "paid", "refunded"]


# ---------------------------------------------------------------------------
# GET /events — list (reverse-chron) + authz
# ---------------------------------------------------------------------------


async def test_list_events_reverse_chron(
    client, user_factory, product_factory, test_shipping_method_id, scheduled_emails
):
    customer = user_factory(email="oe-cust6@example.com")
    admin = user_factory(email="oe-admin6@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id
    )
    await client.patch(
        f"/api/v1/orders/{order['id']}",
        json={"status": "paid", "payment_method": "stripe"},
        headers=_headers(admin),
    )

    resp = await client.get(f"/api/v1/orders/{order['id']}/events", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    # Newest first: paid before created.
    assert [e["kind"] for e in body] == ["paid", "created"]
    assert body[0]["order_id"] == order["id"]


async def test_list_events_authz(client, user_factory, product_factory, test_shipping_method_id):
    customer = user_factory(email="oe-cust7@example.com")
    product = product_factory(stock=10, price_usd=10)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id
    )

    assert (await client.get(f"/api/v1/orders/{order['id']}/events")).status_code == 401
    assert (
        await client.get(f"/api/v1/orders/{order['id']}/events", headers=_headers(customer))
    ).status_code == 403


async def test_list_events_404(client, user_factory):
    admin = user_factory(email="oe-admin8@example.com", role=UserRole.ADMIN)
    resp = await client.get("/api/v1/orders/999999/events", headers=_headers(admin))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /events — staff comment + capability gate
# ---------------------------------------------------------------------------


async def test_staff_comment_created(
    client, user_factory, product_factory, db_session: Session, test_shipping_method_id
):
    customer = user_factory(email="oe-cust9@example.com")
    admin = user_factory(email="oe-admin9@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/orders/{order['id']}/events",
        json={"comment": "  Called customer to confirm address  "},
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "note"
    assert body["comment"] == "Called customer to confirm address"  # trimmed
    assert body["actor"]["email"] == "oe-admin9@example.com"

    # It shows at the top of the timeline.
    listing = await client.get(f"/api/v1/orders/{order['id']}/events", headers=_headers(admin))
    assert listing.json()[0]["kind"] == "note"


async def test_staff_comment_requires_orders_write_capability(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="oe-cust10@example.com")
    product = product_factory(stock=10, price_usd=10)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id
    )

    # Scoped staffer WITHOUT orders:write → 403.
    no_cap = user_factory(
        email="oe-nocap@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[CATALOG_WRITE],
    )
    denied = await client.post(
        f"/api/v1/orders/{order['id']}/events",
        json={"comment": "nope"},
        headers=_headers(no_cap),
    )
    assert denied.status_code == 403

    # Scoped staffer WITH orders:write → 201.
    with_cap = user_factory(
        email="oe-withcap@example.com",
        role=UserRole.ADMIN,
        is_owner=False,
        capabilities=[ORDERS_WRITE],
    )
    allowed = await client.post(
        f"/api/v1/orders/{order['id']}/events",
        json={"comment": "handled"},
        headers=_headers(with_cap),
    )
    assert allowed.status_code == 201


async def test_empty_comment_rejected(
    client, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="oe-cust11@example.com")
    admin = user_factory(email="oe-admin11@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=10)
    order = await _place_order(
        client, customer, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/orders/{order['id']}/events",
        json={"comment": "   "},
        headers=_headers(admin),
    )
    assert resp.status_code == 422
