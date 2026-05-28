"""Integration tests for sequential faktura numbering on the invoice download.

A pending order downloads as a proforma; once an admin flips it to ``paid`` (the
keyless stand-in for a payment webhook) it gains a gap-free series number, which
the download filename reflects and which is stable across re-downloads.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.core.security import create_access_token
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _order_body(product_id: int, quantity: int = 1) -> dict:
    return {
        "items": [{"product_id": product_id, "quantity": quantity}],
        "currency": "USD",
        "shipping_address": "1 Test St",
        "shipping_city": "Testville",
        "shipping_postal_code": "00-001",
        "shipping_country": "Poland",
    }


async def _create_order(client, user, product) -> int:
    resp = await client.post(
        "/api/v1/orders/", json=_order_body(product.id), headers=_headers(user)
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _mark_paid(client, admin, order_id: int) -> None:
    resp = await client.patch(
        f"/api/v1/orders/{order_id}", json={"status": "paid"}, headers=_headers(admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "paid"


async def test_paid_order_download_is_numbered_faktura(client, user_factory, product_factory):
    owner = user_factory(email="fakt-owner@example.com")
    admin = user_factory(email="fakt-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=25.0)
    order_id = await _create_order(client, owner, product)

    await _mark_paid(client, admin, order_id)

    resp = await client.get(f"/api/v1/orders/{order_id}/invoice", headers=_headers(owner))
    assert resp.status_code == 200
    assert resp.content[:4] == b"%PDF"
    disposition = resp.headers["content-disposition"]
    year = dt.datetime.now(dt.UTC).year
    # Series number FV/{year}/00001 → filename sanitised '/' → '-'.
    assert f"invoice-FV-{year}-00001.pdf" in disposition
    assert "proforma" not in disposition

    # Re-download is idempotent: same number, never a new one.
    again = await client.get(f"/api/v1/orders/{order_id}/invoice", headers=_headers(owner))
    assert again.headers["content-disposition"] == disposition


async def test_sequential_numbers_across_paid_orders(client, user_factory, product_factory):
    owner = user_factory(email="seq-owner@example.com")
    admin = user_factory(email="seq-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=25.0)
    year = dt.datetime.now(dt.UTC).year

    first_id = await _create_order(client, owner, product)
    await _mark_paid(client, admin, first_id)
    second_id = await _create_order(client, owner, product)
    await _mark_paid(client, admin, second_id)

    first = await client.get(f"/api/v1/orders/{first_id}/invoice", headers=_headers(owner))
    second = await client.get(f"/api/v1/orders/{second_id}/invoice", headers=_headers(owner))

    assert f"invoice-FV-{year}-00001.pdf" in first.headers["content-disposition"]
    assert f"invoice-FV-{year}-00002.pdf" in second.headers["content-disposition"]


async def test_pending_order_is_proforma_then_faktura_after_paid(
    client, user_factory, product_factory
):
    owner = user_factory(email="trans-owner@example.com")
    admin = user_factory(email="trans-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=25.0)
    order_id = await _create_order(client, owner, product)

    before = await client.get(f"/api/v1/orders/{order_id}/invoice", headers=_headers(owner))
    assert "proforma-ORD-" in before.headers["content-disposition"]

    await _mark_paid(client, admin, order_id)

    after = await client.get(f"/api/v1/orders/{order_id}/invoice", headers=_headers(owner))
    assert "invoice-FV-" in after.headers["content-disposition"]
    assert "proforma" not in after.headers["content-disposition"]
