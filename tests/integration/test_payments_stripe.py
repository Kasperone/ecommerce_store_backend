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


def _make_pending_order(
    db, user, product, *, shipping_method_id: int, quantity: int = 2, currency: str = "USD"
):
    obj_in = OrderCreate(
        items=[OrderItemCreate(product_id=product.id, quantity=quantity)],
        currency=currency,
        shipping_address="1 Test St",
        shipping_city="Town",
        shipping_postal_code="00-001",
        shipping_country="US",
        shipping_method_id=shipping_method_id,
    )
    order = crud_order.create_with_items(db, user_id=user.id, obj_in=obj_in)
    db.commit()
    db.refresh(order)
    return order


def _make_guest_pending_order(
    db, product, *, shipping_method_id: int, quantity: int = 2, currency: str = "USD"
):
    """A pending guest order (user_id NULL, guest_token issued)."""
    obj_in = OrderCreate(
        items=[OrderItemCreate(product_id=product.id, quantity=quantity)],
        currency=currency,
        shipping_address="1 Guest St",
        shipping_city="Town",
        shipping_postal_code="00-001",
        shipping_country="US",
        shipping_method_id=shipping_method_id,
        guest_email="guest@example.com",
    )
    order = crud_order.create_with_items(db, user_id=None, obj_in=obj_in)
    db.commit()
    db.refresh(order)
    return order


def _completed_event(
    order_number: str,
    payment_status: str = "paid",
    pi: str = "pi_123",
    amount_total: int | None = None,
    currency: str | None = None,
) -> bytes:
    obj = {
        "client_reference_id": order_number,
        "payment_status": payment_status,
        "payment_intent": pi,
        "metadata": {"order_number": order_number},
    }
    # Only present when the test exercises the amount/currency assertion; absent
    # keeps the lenient (signature-is-the-boundary) path that other tests use.
    if amount_total is not None:
        obj["amount_total"] = amount_total
    if currency is not None:
        obj["currency"] = currency
    return json.dumps(
        {
            "id": "evt_1",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {"object": obj},
        }
    ).encode("utf-8")


@pytest.fixture
def configured_stripe(monkeypatch):
    """Set non-empty Stripe settings so get_stripe_service doesn't 503."""
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)


def _payment_intent_succeeded_event(order_number: str, pi: str = "pi_123") -> bytes:
    """A Stripe payment_intent.succeeded event — the in-page wallet settle path.

    The event object IS the PaymentIntent, so the id lives at ``object.id`` and
    our order_number rides in ``object.metadata`` (no client_reference_id here).
    """
    return json.dumps(
        {
            "id": "evt_pi_1",
            "object": "event",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": pi, "metadata": {"order_number": order_number}}},
        }
    ).encode("utf-8")


@pytest.fixture
def configured_stripe_full(configured_stripe, monkeypatch):
    """configured_stripe plus a publishable key (the config endpoint returns it)."""
    monkeypatch.setattr(settings, "STRIPE_PUBLISHABLE_KEY", "pk_test_x")


@pytest.fixture
def unconfigured_stripe(monkeypatch):
    """Force Stripe settings empty so the suite's ambient .env can't configure it."""
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "")


@pytest.fixture
def fake_session(monkeypatch):
    """Stub the Stripe SDK's create call with a fake hosted-checkout session.

    Yields the list of captured call kwargs so tests can assert on what the
    service sent to Stripe (e.g. success_url / cancel_url shapes).
    """
    calls: list[dict] = []

    def _create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(url="https://checkout.stripe.com/c/pay/cs_test_1", id="cs_test_1")

    monkeypatch.setattr(stripe.checkout.Session, "create", _create)
    return calls


@pytest.fixture
def fake_payment_intent(monkeypatch):
    """Stub the Stripe SDK's PaymentIntent.create with a fake intent."""
    monkeypatch.setattr(
        stripe.PaymentIntent,
        "create",
        lambda **k: SimpleNamespace(client_secret="pi_test_1_secret_abc", id="pi_test_1"),
    )


# ---------------------------------------------------------------------------
# POST /payments/stripe/checkout/{id}  (create checkout)
# ---------------------------------------------------------------------------


async def test_create_checkout_returns_url(
    client,
    user_factory,
    product_factory,
    db_session,
    configured_stripe,
    fake_session,
    test_shipping_method_id,
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=499.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(f"/api/v1/payments/stripe/checkout/{order.id}", headers=_headers(user))

    assert resp.status_code == 200
    assert resp.json()["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_test_1"
    # Authed orders keep the exact pre-guest URLs — no track token anywhere.
    frontend = settings.FRONTEND_URL.rstrip("/")
    sent = fake_session[-1]
    assert sent["success_url"] == (f"{frontend}/checkout/payment/return?order={order.order_number}")
    assert sent["cancel_url"] == f"{frontend}/account/orders/{order.id}"


async def test_create_checkout_503_when_unconfigured(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(f"/api/v1/payments/stripe/checkout/{order.id}", headers=_headers(user))
    assert resp.status_code == 503


async def test_create_checkout_401_without_auth(client):
    resp = await client.post("/api/v1/payments/stripe/checkout/1")
    assert resp.status_code == 401


async def test_create_checkout_404_for_other_users_order(
    client,
    user_factory,
    product_factory,
    db_session,
    configured_stripe,
    fake_session,
    test_shipping_method_id,
):
    owner = user_factory()
    other = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(
        db_session, owner, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/payments/stripe/checkout/{order.id}", headers=_headers(other)
    )
    assert resp.status_code == 404


async def test_create_checkout_guest_token_ok(
    client,
    product_factory,
    db_session,
    configured_stripe,
    fake_session,
    test_shipping_method_id,
):
    """An anonymous caller starts payment for a guest order with its guest_token."""
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_guest_pending_order(
        db_session, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/payments/stripe/checkout/{order.id}",
        params={"guest_token": order.guest_token},
    )
    assert resp.status_code == 200
    assert resp.json()["checkout_url"] == "https://checkout.stripe.com/c/pay/cs_test_1"
    # The hosted-redirect return URL carries the guest track token so the
    # return page can offer the "view order" CTA (guests have no /account);
    # cancel goes back to the token track view.
    frontend = settings.FRONTEND_URL.rstrip("/")
    sent = fake_session[-1]
    assert sent["success_url"] == (
        f"{frontend}/checkout/payment/return?order={order.order_number}&token={order.guest_token}"
    )
    assert sent["cancel_url"] == f"{frontend}/orders/track/{order.guest_token}"


async def test_create_checkout_guest_wrong_token_404(
    client,
    product_factory,
    db_session,
    configured_stripe,
    fake_session,
    test_shipping_method_id,
):
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_guest_pending_order(
        db_session, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/payments/stripe/checkout/{order.id}",
        params={"guest_token": "wrong-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    )
    assert resp.status_code == 404


async def test_create_checkout_guest_token_cannot_reach_authed_order_404(
    client,
    user_factory,
    product_factory,
    db_session,
    configured_stripe,
    fake_session,
    test_shipping_method_id,
):
    # An authed user's order has guest_token NULL — no token value matches.
    owner = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(
        db_session, owner, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/payments/stripe/checkout/{order.id}",
        params={"guest_token": "any-guess-at-all-aaaaaaaaaaaaaaaaaaaaaaaaaa"},
    )
    assert resp.status_code == 404


async def test_create_checkout_404_for_other_customers_guest_order(
    client,
    user_factory,
    product_factory,
    db_session,
    configured_stripe,
    fake_session,
    test_shipping_method_id,
):
    # An authed customer (no token) can't pay a guest order — it isn't theirs.
    other = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_guest_pending_order(
        db_session, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/payments/stripe/checkout/{order.id}", headers=_headers(other)
    )
    assert resp.status_code == 404


async def test_create_checkout_409_when_not_pending(
    client,
    user_factory,
    product_factory,
    db_session,
    configured_stripe,
    fake_session,
    test_shipping_method_id,
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )
    crud_order.cancel(db_session, order=order)
    db_session.commit()

    resp = await client.post(f"/api/v1/payments/stripe/checkout/{order.id}", headers=_headers(user))
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /payments/stripe/webhook
# ---------------------------------------------------------------------------


async def test_webhook_marks_order_paid(
    client, user_factory, product_factory, db_session, configured_stripe, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

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


async def test_webhook_settles_when_amount_and_currency_match(
    client, user_factory, product_factory, db_session, configured_stripe, test_shipping_method_id
):
    # SEC-006: a present, matching amount_total + currency settles as normal.
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    body = _completed_event(
        order.order_number, amount_total=round(order.total * 100), currency="usd"
    )
    resp = await client.post(
        "/api/v1/payments/stripe/webhook",
        content=body,
        headers={"Stripe-Signature": _stripe_sig(body)},
    )

    assert resp.status_code == 200
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PAID


async def test_webhook_refuses_settle_on_amount_mismatch(
    client, user_factory, product_factory, db_session, configured_stripe, test_shipping_method_id
):
    # SEC-006: a verified webhook whose paid amount != order total is refused.
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    body = _completed_event(
        order.order_number, amount_total=round(order.total * 100) - 500, currency="usd"
    )
    resp = await client.post(
        "/api/v1/payments/stripe/webhook",
        content=body,
        headers={"Stripe-Signature": _stripe_sig(body)},
    )

    assert resp.status_code == 200  # acked so Stripe stops retrying
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PENDING  # not settled


async def test_webhook_refuses_settle_on_currency_mismatch(
    client, user_factory, product_factory, db_session, configured_stripe, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    body = _completed_event(
        order.order_number, amount_total=round(order.total * 100), currency="eur"
    )
    resp = await client.post(
        "/api/v1/payments/stripe/webhook",
        content=body,
        headers={"Stripe-Signature": _stripe_sig(body)},
    )

    assert resp.status_code == 200
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PENDING


async def test_webhook_rejects_invalid_signature(
    client, user_factory, product_factory, db_session, configured_stripe, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    body = _completed_event(order.order_number)
    resp = await client.post(
        "/api/v1/payments/stripe/webhook",
        content=body,
        headers={"Stripe-Signature": "t=1,v1=deadbeef"},
    )

    assert resp.status_code == 400
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PENDING


async def test_webhook_malformed_but_signed_payload_returns_400(client, configured_stripe):
    # A signature-VALID body missing the top-level ``object`` field makes the Stripe SDK's
    # construct_event read event.object -> AttributeError. The route must degrade to 400,
    # never 500 (a 500 would also make Stripe retry). Regression for the 2026-06-02 finding
    # notes/plans/test/to-fix/2026-06-02-stripe-webhook-500-malformed-signed-payload-backend.md
    body = json.dumps({"type": "checkout.session.completed", "data": {"object": {}}}).encode(
        "utf-8"
    )
    resp = await client.post(
        "/api/v1/payments/stripe/webhook",
        content=body,
        headers={"Stripe-Signature": _stripe_sig(body)},
    )
    assert resp.status_code == 400


async def test_webhook_ignores_unpaid_session(
    client, user_factory, product_factory, db_session, configured_stripe, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

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
    client, user_factory, product_factory, db_session, configured_stripe, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    body = _completed_event(order.order_number)
    headers = {"Stripe-Signature": _stripe_sig(body)}

    first = await client.post("/api/v1/payments/stripe/webhook", content=body, headers=headers)
    second = await client.post("/api/v1/payments/stripe/webhook", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PAID


# ---------------------------------------------------------------------------
# GET /payments/stripe/config  (browser Express Checkout config — never 503s)
# ---------------------------------------------------------------------------


async def test_stripe_config_reports_configured(client, user_factory, configured_stripe_full):
    user = user_factory()
    resp = await client.get("/api/v1/payments/stripe/config", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json() == {"configured": True, "publishable_key": "pk_test_x"}


async def test_stripe_config_reports_unconfigured(client, user_factory, unconfigured_stripe):
    user = user_factory()
    resp = await client.get("/api/v1/payments/stripe/config", headers=_headers(user))
    # Never 503 — the frontend hides the wallet widget on configured:false.
    assert resp.status_code == 200
    assert resp.json()["configured"] is False


async def test_stripe_config_public_for_guests(client, configured_stripe_full):
    # Public since guest checkout: a guest's payment-method picker needs the
    # (browser-safe) publishable key to mount the wallet widget. Was a 401.
    resp = await client.get("/api/v1/payments/stripe/config")
    assert resp.status_code == 200
    assert resp.json() == {"configured": True, "publishable_key": "pk_test_x"}


# ---------------------------------------------------------------------------
# POST /payments/stripe/payment-intent/{id}  (create PaymentIntent for wallets)
# ---------------------------------------------------------------------------


async def test_create_payment_intent_returns_client_secret(
    client,
    user_factory,
    product_factory,
    db_session,
    configured_stripe,
    fake_payment_intent,
    test_shipping_method_id,
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=499.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/payments/stripe/payment-intent/{order.id}", headers=_headers(user)
    )

    assert resp.status_code == 200
    assert resp.json()["client_secret"] == "pi_test_1_secret_abc"


async def test_create_payment_intent_503_when_unconfigured(
    client, user_factory, product_factory, db_session, unconfigured_stripe, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/payments/stripe/payment-intent/{order.id}", headers=_headers(user)
    )
    assert resp.status_code == 503


async def test_create_payment_intent_401_without_auth(client):
    resp = await client.post("/api/v1/payments/stripe/payment-intent/1")
    assert resp.status_code == 401


async def test_create_payment_intent_guest_token_ok(
    client,
    product_factory,
    db_session,
    configured_stripe,
    fake_payment_intent,
    test_shipping_method_id,
):
    """The wallet (PaymentIntent) path accepts the guest_token the same way."""
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_guest_pending_order(
        db_session, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/payments/stripe/payment-intent/{order.id}",
        params={"guest_token": order.guest_token},
    )
    assert resp.status_code == 200
    assert resp.json()["client_secret"] == "pi_test_1_secret_abc"


async def test_create_payment_intent_guest_wrong_token_404(
    client,
    product_factory,
    db_session,
    configured_stripe,
    fake_payment_intent,
    test_shipping_method_id,
):
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_guest_pending_order(
        db_session, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/payments/stripe/payment-intent/{order.id}",
        params={"guest_token": "wrong-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    )
    assert resp.status_code == 404


async def test_create_payment_intent_404_for_other_users_order(
    client,
    user_factory,
    product_factory,
    db_session,
    configured_stripe,
    fake_payment_intent,
    test_shipping_method_id,
):
    owner = user_factory()
    other = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(
        db_session, owner, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.post(
        f"/api/v1/payments/stripe/payment-intent/{order.id}", headers=_headers(other)
    )
    assert resp.status_code == 404


async def test_create_payment_intent_409_when_not_pending(
    client,
    user_factory,
    product_factory,
    db_session,
    configured_stripe,
    fake_payment_intent,
    test_shipping_method_id,
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )
    crud_order.cancel(db_session, order=order)
    db_session.commit()

    resp = await client.post(
        f"/api/v1/payments/stripe/payment-intent/{order.id}", headers=_headers(user)
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# POST /payments/stripe/webhook — payment_intent.succeeded (wallets settle path)
# ---------------------------------------------------------------------------


async def test_webhook_payment_intent_succeeded_marks_paid(
    client, user_factory, product_factory, db_session, configured_stripe, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=10.0)
    order = _make_pending_order(
        db_session, user, product, shipping_method_id=test_shipping_method_id
    )

    body = _payment_intent_succeeded_event(order.order_number, pi="pi_999")
    resp = await client.post(
        "/api/v1/payments/stripe/webhook",
        content=body,
        headers={"Stripe-Signature": _stripe_sig(body), "Content-Type": "application/json"},
    )

    assert resp.status_code == 200
    db_session.refresh(order)
    assert OrderStatus(order.status) == OrderStatus.PAID
    assert order.payment_method == "stripe"
    assert order.payment_intent_id == "pi_999"


async def test_webhook_payment_intent_succeeded_unknown_order_is_noop(
    client, db_session, configured_stripe
):
    body = _payment_intent_succeeded_event("ORD-DOES-NOT-EXIST")
    resp = await client.post(
        "/api/v1/payments/stripe/webhook",
        content=body,
        headers={"Stripe-Signature": _stripe_sig(body)},
    )
    assert resp.status_code == 200
