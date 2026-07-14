"""Integration tests for the D6 refund destination (store credit).

``RefundCreate.destination`` defaults to ``"original"`` (record-only — exactly
the pre-D6 behavior). ``"store_credit"`` credits the order's user account in
the same transaction as the refund row (ledger kind ``refund``, linked by
``refund_id`` — the link ``Refund.destination`` derives from) and 400s with the
``store_credit_requires_account`` machine code on guest orders.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.security import create_access_token
from app.crud import store_credit as crud_store_credit
from app.models.store_credit import StoreCreditEntry
from app.models.user import UserRole
from app.services import order_email

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _stub_order_emails(monkeypatch):
    """The refund route schedules a background email whose own DB session can't
    see the test's savepoint rows — stub it (mirrors test_refunds.py)."""
    monkeypatch.setattr(order_email, "send_order_event_in_background", lambda order_id, event: None)


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _order_body(product_id: int, *, shipping_method_id: int, **overrides) -> dict:
    body = {
        "items": [{"product_id": product_id, "quantity": 1}],
        "currency": "PLN",
        "shipping_address": "1 Test St",
        "shipping_city": "Warsaw",
        "shipping_postal_code": "00-001",
        "shipping_country": "PL",
        "shipping_method_id": shipping_method_id,
    }
    body.update(overrides)
    return body


async def _place_and_pay(client, admin, customer, product, *, shipping_method_id: int) -> dict:
    """Pending order as the customer → PAID via admin PATCH (issues the faktura)."""
    placed = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=shipping_method_id),
        headers=_headers(customer),
    )
    assert placed.status_code == 201, placed.text
    order = placed.json()
    paid = await client.patch(
        f"/api/v1/orders/{order['id']}", json={"status": "paid"}, headers=_headers(admin)
    )
    assert paid.status_code == 200, paid.text
    return paid.json()


async def test_refund_to_store_credit_credits_account(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    admin = user_factory(email="rsc-admin1@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="rsc-cust1@example.com")
    product = product_factory(stock=5, price_pln=80.0)
    order = await _place_and_pay(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/orders/{order['id']}/refunds",
        json={"amount": 30.0, "reason": "customer_request", "destination": "store_credit"},
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["destination"] == "store_credit"

    account = crud_store_credit.get_account(db_session, user_id=customer.id, currency="PLN")
    assert account is not None
    assert account.balance == Decimal("30.00")
    entry = (
        db_session.query(StoreCreditEntry).filter(StoreCreditEntry.refund_id == body["id"]).one()
    )
    assert entry.kind == "refund"
    assert entry.amount == Decimal("30.00")
    assert entry.order_id == order["id"]

    # The refund list + order detail carry the derived destination too.
    listed = (
        await client.get(f"/api/v1/orders/{order['id']}/refunds", headers=_headers(admin))
    ).json()
    assert listed[0]["destination"] == "store_credit"


async def test_refund_default_destination_is_record_only(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    admin = user_factory(email="rsc-admin2@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="rsc-cust2@example.com")
    product = product_factory(stock=5, price_pln=80.0)
    order = await _place_and_pay(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/orders/{order['id']}/refunds",
        json={"amount": 10.0, "reason": "other"},
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    assert resp.json()["destination"] == "original"
    # No account was created, no ledger row written.
    assert crud_store_credit.get_account(db_session, user_id=customer.id, currency="PLN") is None
    assert (
        db_session.query(StoreCreditEntry).filter(StoreCreditEntry.order_id == order["id"]).count()
        == 0
    )


async def test_refund_to_store_credit_guest_order_400(
    client, user_factory, product_factory, db_session, test_shipping_method_id, reset_limiter
):
    admin = user_factory(email="rsc-admin3@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=5, price_pln=80.0)
    placed = await client.post(
        "/api/v1/orders/",
        json=_order_body(
            product.id,
            shipping_method_id=test_shipping_method_id,
            guest_email="rsc-guest@example.com",
        ),
    )
    assert placed.status_code == 201
    order = placed.json()
    paid = await client.patch(
        f"/api/v1/orders/{order['id']}", json={"status": "paid"}, headers=_headers(admin)
    )
    assert paid.status_code == 200

    resp = await client.post(
        f"/api/v1/orders/{order['id']}/refunds",
        json={"amount": 10.0, "reason": "other", "destination": "store_credit"},
        headers=_headers(admin),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "store_credit_requires_account"
    # Nothing was written: no refund row, no ledger.
    db_session.rollback()
    listed = (
        await client.get(f"/api/v1/orders/{order['id']}/refunds", headers=_headers(admin))
    ).json()
    assert listed == []


async def test_refund_destination_validates_literal(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    admin = user_factory(email="rsc-admin4@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="rsc-cust4@example.com")
    product = product_factory(stock=5, price_pln=80.0)
    order = await _place_and_pay(
        client, admin, customer, product, shipping_method_id=test_shipping_method_id
    )
    resp = await client.post(
        f"/api/v1/orders/{order['id']}/refunds",
        json={"amount": 10.0, "reason": "other", "destination": "paypal"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422
