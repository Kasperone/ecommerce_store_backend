"""HTTP-level acceptance for the ``ProductUpdate`` at-least-one-currency rule.

A raw client (or `curl`) that PUTs all three currencies as ``null`` must get
a clean 422 from the API boundary instead of slipping past Pydantic and
tripping the DB CHECK constraint as a 500. Partial patches that omit prices
must still succeed.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _admin_headers(user_factory) -> dict:
    admin = user_factory(role=UserRole.ADMIN, email="admin-products-update@test")
    token, _, _ = create_access_token(subject=str(admin.id))
    return {"Authorization": f"Bearer {token}"}


async def test_put_all_three_currencies_null_returns_422(client, user_factory, product_factory):
    headers = _admin_headers(user_factory)
    product = product_factory()
    response = await client.put(
        f"/api/v1/products/{product.id}",
        json={"price_usd": None, "price_pln": None, "price_eur": None},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    body = response.json()
    detail = str(body.get("detail"))
    assert "At least one price is required" in detail


async def test_put_partial_patch_without_prices_succeeds(client, user_factory, product_factory):
    headers = _admin_headers(user_factory)
    product = product_factory()
    response = await client.put(
        f"/api/v1/products/{product.id}",
        json={"name": "Renamed"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed"


async def test_put_clears_one_keeps_others_succeeds(client, user_factory, product_factory):
    headers = _admin_headers(user_factory)
    product = product_factory()
    response = await client.put(
        f"/api/v1/products/{product.id}",
        json={"price_usd": None, "price_pln": 50.0},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["price_usd"] is None
    assert response.json()["price_pln"] == 50.0


# ---------------------------------------------------------------------------
# low_stock_threshold round-trip — the field the admin inventory adjust dialog
# and the product edit page both write through PUT /products/{id}.
# ---------------------------------------------------------------------------


async def test_put_sets_and_returns_low_stock_threshold(client, user_factory, product_factory):
    headers = _admin_headers(user_factory)
    product = product_factory(low_stock_threshold=None)

    response = await client.put(
        f"/api/v1/products/{product.id}",
        json={"low_stock_threshold": 5},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["low_stock_threshold"] == 5

    # Round-trips on GET too (the product edit form reads it there).
    fetched = await client.get(f"/api/v1/products/{product.id}")
    assert fetched.status_code == 200
    assert fetched.json()["low_stock_threshold"] == 5


async def test_put_clears_low_stock_threshold(client, user_factory, product_factory):
    headers = _admin_headers(user_factory)
    product = product_factory(low_stock_threshold=7)

    response = await client.put(
        f"/api/v1/products/{product.id}",
        json={"low_stock_threshold": None},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["low_stock_threshold"] is None


# ---------------------------------------------------------------------------
# Whitespace-only name rejection (finding B-1) — ``Field(min_length=1)`` alone
# accepts ``"   "``; ``ProductName`` adds ``strip_whitespace`` so a blank-only
# name 422s and a padded name is stored trimmed. Covers create + update + name_pl.
# ---------------------------------------------------------------------------


async def test_post_whitespace_only_name_returns_422(client, user_factory):
    headers = _admin_headers(user_factory)
    response = await client.post(
        "/api/v1/products/",
        json={"name": "   ", "slug": "ws-name", "price_usd": 10.0},
        headers=headers,
    )
    assert response.status_code == 422, response.text


async def test_put_whitespace_only_name_returns_422(client, user_factory, product_factory):
    headers = _admin_headers(user_factory)
    product = product_factory()
    response = await client.put(
        f"/api/v1/products/{product.id}",
        json={"name": "   "},
        headers=headers,
    )
    assert response.status_code == 422, response.text


async def test_put_whitespace_only_name_pl_returns_422(client, user_factory, product_factory):
    headers = _admin_headers(user_factory)
    product = product_factory()
    response = await client.put(
        f"/api/v1/products/{product.id}",
        json={"name_pl": "   "},
        headers=headers,
    )
    assert response.status_code == 422, response.text


async def test_put_padded_name_is_trimmed(client, user_factory, product_factory):
    headers = _admin_headers(user_factory)
    product = product_factory()
    response = await client.put(
        f"/api/v1/products/{product.id}",
        json={"name": "  Trimmed Name  "},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Trimmed Name"
