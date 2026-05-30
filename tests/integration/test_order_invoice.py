"""Integration tests for the order invoice PDF endpoint.

GET /api/v1/orders/{id}/invoice — scoped like the detail route (owner or admin),
returns an application/pdf body. Order-invoice plan:
``ecommerce_store/.claude/notes/plans/`` (linear-conjuring-lark).
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _order_body(
    product_id: int, *, shipping_method_id: int, quantity: int = 1, currency: str = "USD"
) -> dict:
    return {
        "items": [{"product_id": product_id, "quantity": quantity}],
        "currency": currency,
        "shipping_address": "1 Test St",
        "shipping_city": "Testville",
        "shipping_postal_code": "00-001",
        "shipping_country": "PL",
        "shipping_method_id": shipping_method_id,
    }


async def _create_order(
    client, user, product, *, shipping_method_id: int, quantity: int = 1, currency: str = "USD"
) -> int:
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(
            product.id, quantity=quantity, currency=currency, shipping_method_id=shipping_method_id
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def test_owner_downloads_invoice_pdf(
    client, user_factory, product_factory, test_shipping_method_id
):
    owner = user_factory(email="inv-owner@example.com")
    product = product_factory(stock=10, price_usd=25.0)
    order_id = await _create_order(
        client, owner, product, shipping_method_id=test_shipping_method_id, quantity=2
    )

    # A freshly-created order is pending → proforma (no series number assigned yet).
    resp = await client.get(f"/api/v1/orders/{order_id}/invoice", headers=_headers(owner))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert "proforma-PF-" in disposition
    assert resp.content[:4] == b"%PDF"


async def test_admin_downloads_any_invoice(
    client, user_factory, product_factory, test_shipping_method_id
):
    owner = user_factory(email="inv-a-owner@example.com")
    admin = user_factory(email="inv-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=25.0)
    order_id = await _create_order(
        client, owner, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.get(f"/api/v1/orders/{order_id}/invoice", headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.content[:4] == b"%PDF"


async def test_cross_customer_invoice_404(
    client, user_factory, product_factory, test_shipping_method_id
):
    owner = user_factory(email="inv-x-owner@example.com")
    other = user_factory(email="inv-x-other@example.com")
    product = product_factory(stock=10, price_usd=25.0)
    order_id = await _create_order(
        client, owner, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.get(f"/api/v1/orders/{order_id}/invoice", headers=_headers(other))
    assert resp.status_code == 404


async def test_invoice_requires_auth(
    client, user_factory, product_factory, test_shipping_method_id
):
    owner = user_factory(email="inv-anon-owner@example.com")
    product = product_factory(stock=10, price_usd=25.0)
    order_id = await _create_order(
        client, owner, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.get(f"/api/v1/orders/{order_id}/invoice")
    assert resp.status_code == 401


async def test_invoice_polish_lang(client, user_factory, product_factory, test_shipping_method_id):
    owner = user_factory(email="inv-pl-owner@example.com")
    product = product_factory(stock=10)
    order_id = await _create_order(
        client, owner, product, shipping_method_id=test_shipping_method_id, currency="PLN"
    )

    resp = await client.get(f"/api/v1/orders/{order_id}/invoice?lang=pl", headers=_headers(owner))
    assert resp.status_code == 200
    assert resp.content[:4] == b"%PDF"


async def test_invoice_invalid_lang_422(
    client, user_factory, product_factory, test_shipping_method_id
):
    owner = user_factory(email="inv-badlang-owner@example.com")
    product = product_factory(stock=10, price_usd=25.0)
    order_id = await _create_order(
        client, owner, product, shipping_method_id=test_shipping_method_id
    )

    resp = await client.get(f"/api/v1/orders/{order_id}/invoice?lang=fr", headers=_headers(owner))
    assert resp.status_code == 422
