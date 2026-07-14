"""Integration tests for gift-card + store-credit tenders at checkout (BE PR2).

Covers the D2/D10 wiring onto the PR1 seams:

- ``POST /orders/`` with ``gift_card_code`` / ``use_store_credit`` — partial and
  full coverage (authed + guest), combined card+credit, the D10 application
  order, ledger debits keyed by order id, snapshot columns, and the wire fields
  (``gift_card_amount`` / ``store_credit_amount`` / ``amount_due`` /
  ``gift_card_last4``) on OrderResponse + the guest track view;
- fail-loud machine codes mirroring the public check endpoint: unknown code
  404s the create (``gift_card_not_found``); disabled/expired/depleted/
  wrong-currency cards 409, toggle-off 409s (``gift_cards_disabled`` /
  ``store_credit_disabled``), guest ``use_store_credit`` 400s
  (``store_credit_requires_account``);
- fully covered → PAID at creation in the same transaction (faktura issued,
  ``payment_method`` per D10, ``payment_intent_id`` stays NULL) — incl. a COD
  order; payment-start then 409s (already-paid gate);
- cancel releases both tenders (ledger ``release`` rows; snapshots stay); a
  rolled-back create burns nothing;
- providers charge ``amount_due``: PayU totalAmount + the webhook
  amount-verification settle/refuse pair.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal

import httpx
import pytest
import respx

from app.core.config import settings
from app.core.security import create_access_token
from app.crud import gift_card as crud_gift_card
from app.crud import invoice as crud_invoice
from app.crud import store_config as crud_store_config
from app.crud import store_credit as crud_store_credit
from app.models.gift_card import GiftCardEntry
from app.models.order import Order, OrderStatus
from app.models.store_credit import StoreCreditEntry

pytestmark = pytest.mark.integration

ORDERS_URL = "/api/v1/orders/"
PAYU_SANDBOX = "https://secure.snd.payu.com"
PAYU_SECOND_KEY = "md5key"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _payu_sign(body: bytes) -> str:
    return hashlib.md5(body + PAYU_SECOND_KEY.encode("utf-8"), usedforsecurity=False).hexdigest()


@pytest.fixture
def toggles_on(db_session):
    """Enable both stored-value toggles for this test (ships dark by default)."""
    config = crud_store_config.get_singleton(db_session)
    config.gift_cards_enabled = True
    config.store_credit_enabled = True
    db_session.commit()
    return config


@pytest.fixture
def configured_payu(monkeypatch):
    for key, value in {
        "PAYU_POS_ID": "300746",
        "PAYU_CLIENT_ID": "300746",
        "PAYU_CLIENT_SECRET": "secret",
        "PAYU_SECOND_KEY": PAYU_SECOND_KEY,
    }.items():
        monkeypatch.setattr(settings, key, value)


def _issue_card(db, value: str, currency: str = "PLN"):
    card, code = crud_gift_card.issue(db, initial_value=Decimal(value), currency=currency)
    db.commit()
    return card, code


def _grant_credit(db, user, amount: str, currency: str = "PLN"):
    account, _ = crud_store_credit.adjust(
        db, user_id=user.id, currency=currency, amount=Decimal(amount)
    )
    db.commit()
    return account


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


# ---------------------------------------------------------------------------
# tender application (authed)
# ---------------------------------------------------------------------------


async def test_partial_gift_card_coverage(
    client, user_factory, product_factory, db_session, toggles_on, test_shipping_method_id
):
    user = user_factory(email="tender-p1@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    card, code = _issue_card(db_session, "30.00")

    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, gift_card_code=code
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Wire fields (pinned contract): total unchanged, amount_due = the remainder.
    assert body["total"] == 100.0
    assert body["gift_card_amount"] == 30.0
    assert body["store_credit_amount"] == 0.0
    assert body["amount_due"] == 70.0
    assert body["gift_card_last4"] == card.last4
    assert body["status"] == "pending"

    db_session.refresh(card)
    assert card.balance == Decimal("0.00")
    redeem = (
        db_session.query(GiftCardEntry)
        .filter(GiftCardEntry.order_id == body["id"], GiftCardEntry.kind == "redeem")
        .one()
    )
    assert redeem.amount == Decimal("-30.00")


async def test_full_gift_card_coverage_paid_at_creation(
    client, user_factory, product_factory, db_session, toggles_on, test_shipping_method_id
):
    user = user_factory(email="tender-f1@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    card, code = _issue_card(db_session, "250.00")

    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, gift_card_code=code
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "paid"
    assert body["payment_method"] == "gift_card"
    assert body["payment_intent_id"] is None
    assert body["gift_card_amount"] == 100.0
    assert body["amount_due"] == 0.0

    # min(balance, total) — only the total was debited.
    db_session.refresh(card)
    assert card.balance == Decimal("150.00")
    # Gap-free faktura issued in the same transaction (mark_paid machinery).
    assert crud_invoice.get_by_order_id(db_session, body["id"]) is not None


async def test_gift_card_plus_store_credit_combined(
    client, user_factory, product_factory, db_session, toggles_on, test_shipping_method_id
):
    user = user_factory(email="tender-c1@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    _, code = _issue_card(db_session, "60.00")
    account = _grant_credit(db_session, user, "100.00")

    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id,
            shipping_method_id=test_shipping_method_id,
            gift_card_code=code,
            use_store_credit=True,
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # D10 order: card first (min(60, 100)), credit covers the remainder.
    assert body["gift_card_amount"] == 60.0
    assert body["store_credit_amount"] == 40.0
    assert body["amount_due"] == 0.0
    assert body["status"] == "paid"
    # A card contributed → payment_method = "gift_card" (D10).
    assert body["payment_method"] == "gift_card"

    db_session.refresh(account)
    assert account.balance == Decimal("60.00")
    redeem = (
        db_session.query(StoreCreditEntry)
        .filter(StoreCreditEntry.order_id == body["id"], StoreCreditEntry.kind == "redeem")
        .one()
    )
    assert redeem.amount == Decimal("-40.00")


async def test_store_credit_partial_then_full(
    client, user_factory, product_factory, db_session, toggles_on, test_shipping_method_id
):
    user = user_factory(email="tender-sc1@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    account = _grant_credit(db_session, user, "25.00")

    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, use_store_credit=True
        ),
        headers=_headers(user),
    )
    body = resp.json()
    assert resp.status_code == 201
    assert body["store_credit_amount"] == 25.0
    assert body["amount_due"] == 75.0
    assert body["status"] == "pending"
    assert body["gift_card_last4"] is None
    db_session.refresh(account)
    assert account.balance == Decimal("0.00")

    # Top the account up past a second order's total → fully covered.
    _grant_credit(db_session, user, "150.00")
    resp2 = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, use_store_credit=True
        ),
        headers=_headers(user),
    )
    body2 = resp2.json()
    assert body2["status"] == "paid"
    assert body2["payment_method"] == "store_credit"
    assert body2["amount_due"] == 0.0
    db_session.refresh(account)
    assert account.balance == Decimal("50.00")


async def test_use_store_credit_without_account_contributes_zero(
    client, user_factory, product_factory, db_session, toggles_on, test_shipping_method_id
):
    """The checkbox is opportunistic — no account simply means a 0 tender."""
    user = user_factory(email="tender-noacct@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, use_store_credit=True
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["store_credit_amount"] == 0.0
    assert body["amount_due"] == body["total"]
    assert body["status"] == "pending"


async def test_guest_gift_card_redeems(
    client, product_factory, db_session, toggles_on, test_shipping_method_id, reset_limiter
):
    """D8: gift cards are bearer instruments — guests may redeem."""
    product = product_factory(stock=5, price_pln=100.0)
    card, code = _issue_card(db_session, "40.00")
    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id,
            shipping_method_id=test_shipping_method_id,
            gift_card_code=code,
            guest_email="tender-guest@example.com",
        ),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["gift_card_amount"] == 40.0
    assert body["amount_due"] == 60.0
    db_session.refresh(card)
    assert card.balance == Decimal("0.00")

    # The guest track view carries the tender wire fields too.
    track = await client.get(f"/api/v1/orders/track/{body['guest_token']}")
    assert track.status_code == 200
    tbody = track.json()
    assert tbody["gift_card_amount"] == 40.0
    assert tbody["store_credit_amount"] == 0.0
    assert tbody["amount_due"] == 60.0
    assert tbody["gift_card_last4"] == card.last4


async def test_fully_covered_cod_order_goes_paid(
    client, user_factory, product_factory, db_session, toggles_on, test_shipping_method_id
):
    """COD + full card coverage: nothing to collect on delivery → PAID, and the
    payment_method flips from "cod" to the tender per D10."""
    user = user_factory(email="tender-cod@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    _, code = _issue_card(db_session, "100.00")
    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id,
            shipping_method_id=test_shipping_method_id,
            gift_card_code=code,
            payment_method="cod",
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "paid"
    assert body["payment_method"] == "gift_card"
    assert body["amount_due"] == 0.0


# ---------------------------------------------------------------------------
# fail-loud machine codes
# ---------------------------------------------------------------------------


async def test_invalid_code_fails_order_and_burns_nothing(
    client, user_factory, product_factory, db_session, toggles_on, test_shipping_method_id
):
    user = user_factory(email="tender-bad1@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id,
            shipping_method_id=test_shipping_method_id,
            gift_card_code="AAAA-BBBB-CCCC-DDDD",
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "gift_card_not_found"
    # The create rolled back whole: no order row, stock untouched.
    db_session.rollback()
    db_session.refresh(product)
    assert product.stock == 5
    assert db_session.query(Order).filter(Order.user_id == user.id).count() == 0


async def test_wrong_currency_card_409(
    client, user_factory, product_factory, db_session, toggles_on, test_shipping_method_id
):
    user = user_factory(email="tender-cur@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    _, code = _issue_card(db_session, "50.00", currency="USD")
    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, gift_card_code=code
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "gift_card_currency_mismatch"


async def test_disabled_expired_and_depleted_card_409(
    client, user_factory, product_factory, db_session, toggles_on, test_shipping_method_id
):
    user = user_factory(email="tender-dis@example.com")
    product = product_factory(stock=5, price_pln=100.0)

    card, code = _issue_card(db_session, "50.00")
    card.status = "disabled"
    db_session.commit()
    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, gift_card_code=code
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "gift_card_disabled"

    card2, code2 = _issue_card(db_session, "50.00")
    card2.expires_at = datetime(2020, 1, 1)
    db_session.commit()
    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, gift_card_code=code2
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "gift_card_expired"

    card3, code3 = _issue_card(db_session, "50.00")
    crud_gift_card.adjust(db_session, gift_card_id=card3.id, amount=Decimal("-50.00"))
    db_session.commit()
    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, gift_card_code=code3
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "gift_card_depleted"


async def test_toggle_off_gift_card_409(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    # Default store_config: both toggles off (D7).
    user = user_factory(email="tender-t1@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id,
            shipping_method_id=test_shipping_method_id,
            gift_card_code="AAAA-BBBB-CCCC-DDDD",
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "gift_cards_disabled"


async def test_toggle_off_store_credit_409(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    user = user_factory(email="tender-t2@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, use_store_credit=True
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "store_credit_disabled"


async def test_guest_use_store_credit_400(
    client, product_factory, db_session, toggles_on, test_shipping_method_id, reset_limiter
):
    product = product_factory(stock=5, price_pln=100.0)
    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id,
            shipping_method_id=test_shipping_method_id,
            use_store_credit=True,
            guest_email="guest-sc@example.com",
        ),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "store_credit_requires_account"


# ---------------------------------------------------------------------------
# cancel releases + rollback burns nothing
# ---------------------------------------------------------------------------


async def test_cancel_releases_both_tenders(
    client, user_factory, product_factory, db_session, toggles_on, test_shipping_method_id
):
    user = user_factory(email="tender-cx@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    card, code = _issue_card(db_session, "30.00")
    account = _grant_credit(db_session, user, "20.00")

    resp = await client.post(
        ORDERS_URL,
        json=_order_body(
            product.id,
            shipping_method_id=test_shipping_method_id,
            gift_card_code=code,
            use_store_credit=True,
        ),
        headers=_headers(user),
    )
    body = resp.json()
    assert body["status"] == "pending"  # 50 covered, 50 due
    assert body["gift_card_amount"] == 30.0
    assert body["store_credit_amount"] == 20.0

    cancel = await client.post(f"/api/v1/orders/{body['id']}/cancel", headers=_headers(user))
    assert cancel.status_code == 200
    cbody = cancel.json()
    assert cbody["status"] == "cancelled"
    # Snapshots stay on the order for history.
    assert cbody["gift_card_amount"] == 30.0
    assert cbody["store_credit_amount"] == 20.0

    db_session.refresh(card)
    db_session.refresh(account)
    assert card.balance == Decimal("30.00")
    assert account.balance == Decimal("20.00")
    assert (
        db_session.query(GiftCardEntry)
        .filter(GiftCardEntry.order_id == body["id"], GiftCardEntry.kind == "release")
        .count()
        == 1
    )
    assert (
        db_session.query(StoreCreditEntry)
        .filter(StoreCreditEntry.order_id == body["id"], StoreCreditEntry.kind == "release")
        .count()
        == 1
    )


async def test_rolled_back_create_burns_nothing(
    db_session, user_factory, product_factory, toggles_on, test_shipping_method_id
):
    """CRUD-level: the debit rides the order's unit of work — rollback undoes it."""
    from app.crud import order as crud_order
    from app.schemas.order import OrderCreate, OrderItemCreate

    user = user_factory(email="tender-rb@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    card, code = _issue_card(db_session, "100.00")

    obj_in = OrderCreate(
        items=[OrderItemCreate(product_id=product.id, quantity=1)],
        currency="PLN",
        shipping_address="1 Test St",
        shipping_city="Warsaw",
        shipping_postal_code="00-001",
        shipping_country="PL",
        shipping_method_id=test_shipping_method_id,
        gift_card_code=code,
    )
    order = crud_order.create_with_items(db_session, user_id=user.id, obj_in=obj_in)
    assert order.gift_card_amount == Decimal("100.00")
    db_session.rollback()  # route would do this on any later failure

    db_session.refresh(card)
    assert card.balance == Decimal("100.00")
    assert db_session.query(GiftCardEntry).filter(GiftCardEntry.kind == "redeem").count() == 0


# ---------------------------------------------------------------------------
# payments charge amount_due
# ---------------------------------------------------------------------------


async def _partial_tender_order(client, user, product, code, *, shipping_method_id: int) -> dict:
    resp = await client.post(
        ORDERS_URL,
        json=_order_body(product.id, shipping_method_id=shipping_method_id, gift_card_code=code),
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_payment_start_409_when_fully_covered(
    client, user_factory, product_factory, db_session, toggles_on, test_shipping_method_id
):
    """Fully covered orders are PAID at creation — the existing pending-status
    gate blocks payment-start for them (no provider config needed: the gate
    runs before the 503 config check)."""
    user = user_factory(email="tender-ps@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    _, code = _issue_card(db_session, "100.00")
    body = await _partial_tender_order(
        client, user, product, code, shipping_method_id=test_shipping_method_id
    )
    assert body["status"] == "paid"

    resp = await client.post(f"/api/v1/payments/payu/orders/{body['id']}", headers=_headers(user))
    assert resp.status_code == 409
    resp = await client.post(
        f"/api/v1/payments/stripe/checkout/{body['id']}", headers=_headers(user)
    )
    assert resp.status_code == 409


@respx.mock
async def test_payu_charges_amount_due(
    client,
    user_factory,
    product_factory,
    db_session,
    toggles_on,
    configured_payu,
    test_shipping_method_id,
):
    respx.post(f"{PAYU_SANDBOX}/pl/standard/user/oauth/authorize").mock(
        return_value=httpx.Response(200, json={"access_token": "tok"}),
    )
    create_route = respx.post(f"{PAYU_SANDBOX}/api/v2_1/orders").mock(
        return_value=httpx.Response(
            302,
            json={
                "status": {"statusCode": "SUCCESS"},
                "redirectUri": "https://secure.snd.payu.com/pay/abc",
                "orderId": "PAYU-1",
            },
        ),
    )
    user = user_factory(email="tender-payu@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    _, code = _issue_card(db_session, "30.00")
    body = await _partial_tender_order(
        client, user, product, code, shipping_method_id=test_shipping_method_id
    )
    assert body["amount_due"] == 70.0

    resp = await client.post(f"/api/v1/payments/payu/orders/{body['id']}", headers=_headers(user))
    assert resp.status_code == 200
    sent = json.loads(create_route.calls.last.request.content)
    # The provider charges amount_due (7000 minor), NOT the 10000 total.
    assert sent["totalAmount"] == "7000"


async def test_payu_webhook_settles_on_amount_due(
    client,
    user_factory,
    product_factory,
    db_session,
    toggles_on,
    configured_payu,
    test_shipping_method_id,
):
    user = user_factory(email="tender-wh1@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    _, code = _issue_card(db_session, "30.00")
    body = await _partial_tender_order(
        client, user, product, code, shipping_method_id=test_shipping_method_id
    )

    payload = json.dumps(
        {
            "order": {
                "extOrderId": body["order_number"],
                "orderId": "PAYU-7",
                "status": "COMPLETED",
                "totalAmount": "7000",  # amount_due minor, what PayU collected
                "currencyCode": "PLN",
            }
        }
    ).encode("utf-8")
    resp = await client.post(
        "/api/v1/payments/payu/notify",
        content=payload,
        headers={
            "OpenPayU-Signature": f"signature={_payu_sign(payload)};algorithm=MD5",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    order = db_session.get(Order, body["id"])
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PAID


async def test_payu_webhook_refuses_full_total_when_tender_applied(
    client,
    user_factory,
    product_factory,
    db_session,
    toggles_on,
    configured_payu,
    test_shipping_method_id,
):
    """A notification claiming the FULL total was collected mismatches
    amount_due on a tender order → refused (defense-in-depth)."""
    user = user_factory(email="tender-wh2@example.com")
    product = product_factory(stock=5, price_pln=100.0)
    _, code = _issue_card(db_session, "30.00")
    body = await _partial_tender_order(
        client, user, product, code, shipping_method_id=test_shipping_method_id
    )

    payload = json.dumps(
        {
            "order": {
                "extOrderId": body["order_number"],
                "orderId": "PAYU-8",
                "status": "COMPLETED",
                "totalAmount": "10000",  # the pre-tender total — wrong
                "currencyCode": "PLN",
            }
        }
    ).encode("utf-8")
    resp = await client.post(
        "/api/v1/payments/payu/notify",
        content=payload,
        headers={
            "OpenPayU-Signature": f"signature={_payu_sign(payload)};algorithm=MD5",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200  # acked so PayU stops retrying
    order = db_session.get(Order, body["id"])
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PENDING  # not settled
