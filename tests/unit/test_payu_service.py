"""Unit tests for the PayU service — signature verification + order creation.

The PayU HTTP API is mocked with ``respx``; no live calls. Signature checks use
the same MD5(body + second_key) scheme the service verifies.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest
import respx

from app.services.payu import PayUClient, PayUError

SANDBOX = "https://secure.snd.payu.com"


def _client(second_key: str = "md5key") -> PayUClient:
    return PayUClient(
        pos_id="300746",
        client_id="300746",
        client_secret="secret",
        second_key=second_key,
        sandbox=True,
    )


def _sign(body: bytes, key: str) -> str:
    return hashlib.md5(body + key.encode("utf-8"), usedforsecurity=False).hexdigest()


# ---------------------------------------------------------------------------
# verify_notification — the security boundary
# ---------------------------------------------------------------------------


class TestVerifyNotification:
    def test_accepts_a_correctly_signed_body(self):
        body = b'{"order":{"status":"COMPLETED"}}'
        header = f"sender=145;signature={_sign(body, 'md5key')};algorithm=MD5;content=DOCUMENT"
        assert _client().verify_notification(body, header) is True

    def test_rejects_a_tampered_body(self):
        original = b'{"order":{"status":"COMPLETED"}}'
        header = f"signature={_sign(original, 'md5key')};algorithm=MD5"
        assert _client().verify_notification(b'{"order":{"status":"CANCELED"}}', header) is False

    def test_rejects_wrong_key(self):
        body = b"{}"
        header = f"signature={_sign(body, 'ATTACKER')};algorithm=MD5"
        assert _client().verify_notification(body, header) is False

    def test_rejects_missing_header(self):
        assert _client().verify_notification(b"{}", None) is False

    def test_rejects_malformed_header(self):
        assert _client().verify_notification(b"{}", "not-a-signature") is False


# ---------------------------------------------------------------------------
# create_order — mocked PayU REST
# ---------------------------------------------------------------------------


@respx.mock
async def test_create_order_returns_redirect_and_id():
    respx.post(f"{SANDBOX}/pl/standard/user/oauth/authorize").mock(
        return_value=httpx.Response(200, json={"access_token": "tok"}),
    )
    respx.post(f"{SANDBOX}/api/v2_1/orders").mock(
        return_value=httpx.Response(
            302,
            json={
                "status": {"statusCode": "SUCCESS"},
                "redirectUri": "https://secure.snd.payu.com/pay/123",
                "orderId": "PAYU-123",
            },
        ),
    )

    result = await _client().create_order(
        ext_order_id="ORD-1",
        amount_minor=99800,
        currency="PLN",
        description="Order ORD-1",
        buyer_email="a@example.com",
        customer_ip="1.2.3.4",
        products=[{"name": "X", "unitPrice": "99800", "quantity": "1"}],
        notify_url="https://api/notify",
        continue_url="https://app/return",
    )
    assert result == {
        "redirect_uri": "https://secure.snd.payu.com/pay/123",
        "payu_order_id": "PAYU-123",
    }


@respx.mock
async def test_create_order_raises_when_payu_rejects():
    respx.post(f"{SANDBOX}/pl/standard/user/oauth/authorize").mock(
        return_value=httpx.Response(200, json={"access_token": "tok"}),
    )
    respx.post(f"{SANDBOX}/api/v2_1/orders").mock(
        return_value=httpx.Response(200, json={"status": {"statusCode": "ERROR_VALUE_INVALID"}}),
    )
    with pytest.raises(PayUError):
        await _client().create_order(
            ext_order_id="ORD-1",
            amount_minor=1,
            currency="PLN",
            description="d",
            buyer_email="a@example.com",
            customer_ip="1.2.3.4",
            products=[],
            notify_url="https://api/notify",
            continue_url="https://app/return",
        )


@respx.mock
async def test_create_order_raises_when_oauth_fails():
    respx.post(f"{SANDBOX}/pl/standard/user/oauth/authorize").mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"}),
    )
    with pytest.raises(PayUError):
        await _client().create_order(
            ext_order_id="ORD-1",
            amount_minor=1,
            currency="PLN",
            description="d",
            buyer_email="a@example.com",
            customer_ip="1.2.3.4",
            products=[],
            notify_url="https://api/notify",
            continue_url="https://app/return",
        )
