"""Unit tests for the Stripe service — Checkout-Session creation + webhook verify.

The Stripe SDK's ``Session.create`` is monkeypatched (no live API). Webhook
verification uses a real ``Stripe-Signature`` (HMAC-SHA256 of ``timestamp.payload``
against the webhook secret) so the security boundary is genuinely exercised —
mirroring the real-MD5-signature approach in ``test_payu_service.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
import stripe

from app.services.stripe import StripeError, StripeService

SECRET = "sk_test_x"
WEBHOOK_SECRET = "whsec_test"


def _service(webhook_secret: str = WEBHOOK_SECRET) -> StripeService:
    return StripeService(secret_key=SECRET, webhook_secret=webhook_secret)


def _stripe_sig(payload: bytes, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> str:
    """Build a valid Stripe-Signature header for ``payload`` (Stripe's scheme)."""
    ts = timestamp if timestamp is not None else int(time.time())
    signed = f"{ts}.{payload.decode('utf-8')}".encode()
    sig = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


# ---------------------------------------------------------------------------
# create_checkout_session — SDK monkeypatched
# ---------------------------------------------------------------------------


def test_create_checkout_session_returns_url_and_id(monkeypatch):
    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(url="https://checkout.stripe.com/c/pay/cs_test_1", id="cs_test_1")

    monkeypatch.setattr(stripe.checkout.Session, "create", fake_create)

    result = _service().create_checkout_session(
        ext_order_id="ORD-1",
        amount_minor=99800,
        currency="USD",
        description="Order ORD-1",
        buyer_email="a@example.com",
        success_url="https://app/return",
        cancel_url="https://app/cancel",
    )

    assert result == {
        "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_1",
        "session_id": "cs_test_1",
    }
    # The order number rides along as client_reference_id (webhook lookup key),
    # the amount is the integer minor unit, and the currency is lowercased.
    assert captured["client_reference_id"] == "ORD-1"
    assert captured["line_items"][0]["price_data"]["unit_amount"] == 99800
    assert captured["line_items"][0]["price_data"]["currency"] == "usd"


def test_create_checkout_session_raises_on_stripe_error(monkeypatch):
    def boom(**kwargs):
        raise stripe.StripeError("nope")

    monkeypatch.setattr(stripe.checkout.Session, "create", boom)
    with pytest.raises(StripeError):
        _service().create_checkout_session(
            ext_order_id="ORD-1",
            amount_minor=1,
            currency="USD",
            description="d",
            buyer_email="a@example.com",
            success_url="https://app/return",
            cancel_url="https://app/cancel",
        )


def test_create_checkout_session_raises_when_url_missing(monkeypatch):
    monkeypatch.setattr(
        stripe.checkout.Session,
        "create",
        lambda **k: SimpleNamespace(url=None, id="cs_x"),
    )
    with pytest.raises(StripeError):
        _service().create_checkout_session(
            ext_order_id="ORD-1",
            amount_minor=1,
            currency="USD",
            description="d",
            buyer_email="a@example.com",
            success_url="https://app/return",
            cancel_url="https://app/cancel",
        )


# ---------------------------------------------------------------------------
# create_payment_intent — SDK monkeypatched (in-page Express Checkout / wallets)
# ---------------------------------------------------------------------------


def test_create_payment_intent_returns_client_secret_and_id(monkeypatch):
    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(client_secret="pi_test_1_secret_abc", id="pi_test_1")

    monkeypatch.setattr(stripe.PaymentIntent, "create", fake_create)

    result = _service().create_payment_intent(
        ext_order_id="ORD-1",
        amount_minor=99800,
        currency="USD",
        description="Order ORD-1",
        buyer_email="a@example.com",
    )

    assert result == {"client_secret": "pi_test_1_secret_abc", "payment_intent_id": "pi_test_1"}
    # The order number rides in metadata (webhook lookup key), the amount is the
    # integer minor unit, the currency is lowercased, and wallets are enabled via
    # automatic_payment_methods.
    assert captured["metadata"]["order_number"] == "ORD-1"
    assert captured["amount"] == 99800
    assert captured["currency"] == "usd"
    assert captured["automatic_payment_methods"] == {"enabled": True}


def test_create_payment_intent_raises_on_stripe_error(monkeypatch):
    def boom(**kwargs):
        raise stripe.StripeError("nope")

    monkeypatch.setattr(stripe.PaymentIntent, "create", boom)
    with pytest.raises(StripeError):
        _service().create_payment_intent(
            ext_order_id="ORD-1",
            amount_minor=1,
            currency="USD",
            description="d",
            buyer_email="a@example.com",
        )


def test_create_payment_intent_raises_when_client_secret_missing(monkeypatch):
    monkeypatch.setattr(
        stripe.PaymentIntent,
        "create",
        lambda **k: SimpleNamespace(client_secret=None, id="pi_x"),
    )
    with pytest.raises(StripeError):
        _service().create_payment_intent(
            ext_order_id="ORD-1",
            amount_minor=1,
            currency="USD",
            description="d",
            buyer_email="a@example.com",
        )


# ---------------------------------------------------------------------------
# construct_event — the security boundary (real signatures)
# ---------------------------------------------------------------------------


class TestConstructEvent:
    def test_accepts_a_correctly_signed_event(self):
        payload = json.dumps(
            {"object": "event", "type": "checkout.session.completed", "data": {"object": {}}}
        ).encode("utf-8")
        event = _service().construct_event(payload, _stripe_sig(payload))
        assert event["type"] == "checkout.session.completed"

    def test_rejects_a_forged_signature(self):
        payload = b'{"type":"checkout.session.completed"}'
        header = _stripe_sig(payload, secret="whsec_attacker")
        with pytest.raises(StripeError):
            _service().construct_event(payload, header)

    def test_rejects_a_tampered_payload(self):
        original = b'{"type":"checkout.session.completed"}'
        header = _stripe_sig(original)
        with pytest.raises(StripeError):
            _service().construct_event(b'{"type":"other"}', header)

    def test_rejects_missing_header(self):
        with pytest.raises(StripeError):
            _service().construct_event(b"{}", None)

    def test_rejects_malformed_header(self):
        with pytest.raises(StripeError):
            _service().construct_event(b"{}", "not-a-signature")
