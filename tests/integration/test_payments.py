"""Integration tests for the PayU payment endpoints.

create-checkout mocks the PayU REST API via ``respx`` (the ASGI test client is a
different transport, so respx only intercepts the service's outbound calls). The
webhook makes no outbound call — it verifies the ``OpenPayU-Signature`` and
settles the order, so those tests just sign a body with the configured key.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
import respx

from app.core.config import settings
from app.core.security import create_access_token
from app.crud import order as crud_order
from app.models.order import OrderStatus
from app.schemas.order import OrderCreate, OrderItemCreate

pytestmark = pytest.mark.integration

SANDBOX = "https://secure.snd.payu.com"
SECOND_KEY = "md5key"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _sign(body: bytes) -> str:
    return hashlib.md5(body + SECOND_KEY.encode("utf-8"), usedforsecurity=False).hexdigest()


def _make_pending_order(
    db, user, product, *, shipping_method_id: int, quantity: int = 2, currency: str = "PLN"
):
    obj_in = OrderCreate(
        items=[OrderItemCreate(product_id=product.id, quantity=quantity)],
        currency=currency,
        shipping_address="1 Test St",
        shipping_city="Town",
        shipping_postal_code="00-001",
        shipping_country="PL",
        shipping_method_id=shipping_method_id,
    )
    order = crud_order.create_with_items(db, user_id=user.id, obj_in=obj_in)
    db.commit()
    db.refresh(order)
    return order


@pytest.fixture
def configured_payu(monkeypatch):
    """Set non-empty PayU settings so get_payu_client doesn't 503."""
    for key, value in {
        "PAYU_POS_ID": "300746",
        "PAYU_CLIENT_ID": "300746",
        "PAYU_CLIENT_SECRET": "secret",
        "PAYU_SECOND_KEY": SECOND_KEY,
    }.items():
        monkeypatch.setattr(settings, key, value)


# ---------------------------------------------------------------------------
# POST /payments/payu/orders/{id}  (create checkout)
# ---------------------------------------------------------------------------


@respx.mock
async def test_create_checkout_returns_redirect(
    client, user_factory, product_factory, db_session, configured_payu, test_shipping_method_id
):
    respx.post(f"{SANDBOX}/pl/standard/user/oauth/authorize").mock(
        return_value=httpx.Response(200, json={"access_token": "tok"}),
    )
    respx.post(f"{SANDBOX}/api/v2_1/orders").mock(
        return_value=httpx.Response(
            302,
            json={
                "status": {"statusCode": "SUCCESS"},
                "redirectUri": "https://secure.snd.payu.com/pay/abc",
                "orderId": "PAYU-1",
            },
        ),
    )
    user = user_factory()
    product = product_factory(stock=10, price_pln=499.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(f"/api/v1/payments/payu/orders/{order.id}", headers=_headers(user))

    assert resp.status_code == 200
    assert resp.json()["redirect_uri"] == "https://secure.snd.payu.com/pay/abc"


async def test_create_checkout_503_when_unconfigured(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=5, price_pln=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(f"/api/v1/payments/payu/orders/{order.id}", headers=_headers(user))
    assert resp.status_code == 503


async def test_create_checkout_401_without_auth(client):
    resp = await client.post("/api/v1/payments/payu/orders/1")
    assert resp.status_code == 401


async def test_create_checkout_404_for_other_users_order(
    client, user_factory, product_factory, db_session, configured_payu, test_shipping_method_id
):
    owner = user_factory()
    other = user_factory()
    product = product_factory(stock=5, price_pln=10.0)
    order = _make_pending_order(
        db_session, owner, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(f"/api/v1/payments/payu/orders/{order.id}", headers=_headers(other))
    assert resp.status_code == 404


async def test_create_checkout_409_when_not_pending(
    client, user_factory, product_factory, db_session, configured_payu, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=5, price_pln=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )
    crud_order.cancel(db_session, order=order)
    db_session.commit()

    resp = await client.post(f"/api/v1/payments/payu/orders/{order.id}", headers=_headers(user))
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /payments/payu/notify  (webhook)
# ---------------------------------------------------------------------------


async def test_webhook_marks_order_paid(
    client, user_factory, product_factory, db_session, configured_payu, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=5, price_pln=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    body = json.dumps(
        {"order": {"extOrderId": order.order_number, "orderId": "PAYU-9", "status": "COMPLETED"}}
    ).encode("utf-8")
    resp = await client.post(
        "/api/v1/payments/payu/notify",
        content=body,
        headers={
            "OpenPayU-Signature": f"signature={_sign(body)};algorithm=MD5",
            "Content-Type": "application/json",
        },
    )

    assert resp.status_code == 200
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PAID
    assert order.payment_method == "payu"
    assert order.payment_intent_id == "PAYU-9"


async def test_webhook_rejects_invalid_signature(
    client, user_factory, product_factory, db_session, configured_payu, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=5, price_pln=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    body = json.dumps(
        {"order": {"extOrderId": order.order_number, "orderId": "X", "status": "COMPLETED"}}
    ).encode("utf-8")
    resp = await client.post(
        "/api/v1/payments/payu/notify",
        content=body,
        headers={"OpenPayU-Signature": "signature=deadbeef;algorithm=MD5"},
    )

    assert resp.status_code == 400
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PENDING


async def test_webhook_ignores_non_completed_status(
    client, user_factory, product_factory, db_session, configured_payu, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=5, price_pln=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    body = json.dumps(
        {"order": {"extOrderId": order.order_number, "orderId": "X", "status": "CANCELED"}}
    ).encode("utf-8")
    resp = await client.post(
        "/api/v1/payments/payu/notify",
        content=body,
        headers={"OpenPayU-Signature": f"signature={_sign(body)};algorithm=MD5"},
    )

    assert resp.status_code == 200
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PENDING


async def test_webhook_is_idempotent(
    client, user_factory, product_factory, db_session, configured_payu, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=5, price_pln=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    body = json.dumps(
        {"order": {"extOrderId": order.order_number, "orderId": "PAYU-9", "status": "COMPLETED"}}
    ).encode("utf-8")
    headers = {"OpenPayU-Signature": f"signature={_sign(body)};algorithm=MD5"}

    first = await client.post("/api/v1/payments/payu/notify", content=body, headers=headers)
    second = await client.post("/api/v1/payments/payu/notify", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PAID
