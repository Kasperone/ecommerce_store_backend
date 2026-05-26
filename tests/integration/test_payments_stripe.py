"""Integration tests for the Stripe payment endpoints.

create-checkout monkeypatches the Stripe SDK's ``Session.create`` (no live API).
The webhook is verified with a real ``Stripe-Signature`` computed against the
configured webhook secret — same shape as ``test_payments.py`` for PayU.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
import stripe

from app.core.config import settings
from app.core.security import create_access_token
from app.crud import order as crud_order
from app.models.order import OrderStatus
from app.schemas.order import OrderCreate, OrderItemCreate

pytestmark = pytest.mark.integration

WEBHOOK_SECRET = "whsec_test"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _stripe_sig(payload: bytes, secret: str = WEBHOOK_SECRET) -> str:
    ts = int(time.time())
    signed = f"{ts}.{payload.decode('utf-8')}".encode()
    sig = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _make_pending_order(db, user, product, quantity: int = 2, currency: str = "USD"):
    obj_in = OrderCreate(
        items=[OrderItemCreate(product_id=product.id, quantity=quantity)],
        currency=currency,
        shipping_address="1 Test St",
        shipping_city="Town",
        shipping_postal_code="00-001",
        shipping_country="USA",
    )
    order = crud_order.create_with_items(db, user_id=user.id, obj_in=obj_in)
    db.commit()
    db.refresh(order)
    return order


def _completed_event(order_number: str, payment_status: str = "paid", pi: str = "pi_123") -> bytes:
    return json.dumps(
        {
            "id": "evt_1",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": order_number,
                    "payment_status": payment_status,
                    "payment_intent": pi,
                    "metadata": {"order_number": order_number},
                }
            },
        }
    ).encode("utf-8")


@pytest.fixture
def configured_stripe(monkeypatch):
    """Set non-empty Stripe settings so get_stripe_service doesn't 503."""
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)


@pytest.fixture
def fake_session(monkeypatch):
    """Stub the Stripe SDK's create call with a fake hosted-checkout session."""
    monkeypatch.setattr(
        stripe.checkout.Session,
        "create",
        lambda **k: SimpleNamespace(
            url="https://checkout.stripe.com/c/pay/cs_test_1", id="cs_test_1"
        ),
    )


# ---------------------------------------------------------------------------
# POST /payments/stripe/checkout/{id}  (create checkout)
# ---------------------------------------------------------------------------


async def test_create_checkout_returns_url(
    client, user_factory, product_factory, db_session, configured_stripe, fake_session
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=499.0)
    order = _make_pending_order(db_session, user, product)

    resp = await client.post(f"/api/v1/payments/stripe/checkout/{order.id}", headers=_headers(user))

    assert resp.status_code == 200
    assert resp.json()["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_test_1"


async def test_create_checkout_503_when_unconfigured(
    client, user_factory, product_factory, db_session
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(db_session, user, product)

    resp = await client.post(f"/api/v1/payments/stripe/checkout/{order.id}", headers=_headers(user))
    assert resp.status_code == 503


async def test_create_checkout_401_without_auth(client):
    resp = await client.post("/api/v1/payments/stripe/checkout/1")
    assert resp.status_code == 401


async def test_create_checkout_404_for_other_users_order(
    client, user_factory, product_factory, db_session, configured_stripe, fake_session
):
    owner = user_factory()
    other = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(db_session, owner, product)

    resp = await client.post(
        f"/api/v1/payments/stripe/checkout/{order.id}", headers=_headers(other)
    )
    assert resp.status_code == 404


async def test_create_checkout_409_when_not_pending(
    client, user_factory, product_factory, db_session, configured_stripe, fake_session
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(db_session, user, product)
    crud_order.cancel(db_session, order=order)
    db_session.commit()

    resp = await client.post(f"/api/v1/payments/stripe/checkout/{order.id}", headers=_headers(user))
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /payments/stripe/webhook
# ---------------------------------------------------------------------------


async def test_webhook_marks_order_paid(
    client, user_factory, product_factory, db_session, configured_stripe
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(db_session, user, product)

    body = _completed_event(order.order_number)
    resp = await client.post(
        "/api/v1/payments/stripe/webhook",
        content=body,
        headers={"Stripe-Signature": _stripe_sig(body), "Content-Type": "application/json"},
    )

    assert resp.status_code == 200
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PAID
    assert order.payment_method == "stripe"
    assert order.payment_intent_id == "pi_123"


async def test_webhook_rejects_invalid_signature(
    client, user_factory, product_factory, db_session, configured_stripe
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(db_session, user, product)

    body = _completed_event(order.order_number)
    resp = await client.post(
        "/api/v1/payments/stripe/webhook",
        content=body,
        headers={"Stripe-Signature": "t=1,v1=deadbeef"},
    )

    assert resp.status_code == 400
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PENDING


async def test_webhook_ignores_unpaid_session(
    client, user_factory, product_factory, db_session, configured_stripe
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(db_session, user, product)

    body = _completed_event(order.order_number, payment_status="unpaid")
    resp = await client.post(
        "/api/v1/payments/stripe/webhook",
        content=body,
        headers={"Stripe-Signature": _stripe_sig(body)},
    )

    assert resp.status_code == 200
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PENDING


async def test_webhook_ignores_other_event_types(client, db_session, configured_stripe):
    body = json.dumps(
        {"id": "evt_2", "object": "event", "type": "payment_intent.created", "data": {"object": {}}}
    ).encode("utf-8")
    resp = await client.post(
        "/api/v1/payments/stripe/webhook",
        content=body,
        headers={"Stripe-Signature": _stripe_sig(body)},
    )
    assert resp.status_code == 200


async def test_webhook_is_idempotent(
    client, user_factory, product_factory, db_session, configured_stripe
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(db_session, user, product)

    body = _completed_event(order.order_number)
    headers = {"Stripe-Signature": _stripe_sig(body)}

    first = await client.post("/api/v1/payments/stripe/webhook", content=body, headers=headers)
    second = await client.post("/api/v1/payments/stripe/webhook", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PAID
