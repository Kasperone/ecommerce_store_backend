"""Integration tests for the wishlist endpoints (`/api/v1/wishlist`).

Owner-scoped: the user is derived from the bearer token and every lookup is
gated on ``(id, user_id)``, so a cross-user or unknown id on delete returns
**404** (never 403). Add is idempotent — an exact re-add returns the existing
row with 200; a new row is 201; a variant must belong to the product.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.product_variant import ProductVariant

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _make_variant(db_session, product, **overrides) -> ProductVariant:
    """Create a minimal active variant for ``product``."""
    defaults = {
        "product_id": product.id,
        "title": "Red / M",
        "option_signature": f"sig-{product.id}",
        "stock": 5,
        "is_active": True,
    }
    defaults.update(overrides)
    variant = ProductVariant(**defaults)
    db_session.add(variant)
    db_session.commit()
    db_session.refresh(variant)
    return variant


# --- auth ------------------------------------------------------------------


async def test_list_requires_auth(client):
    resp = await client.get("/api/v1/wishlist")
    assert resp.status_code == 401


async def test_add_requires_auth(client):
    resp = await client.post("/api/v1/wishlist", json={"product_id": 1})
    assert resp.status_code == 401


async def test_delete_requires_auth(client):
    resp = await client.delete("/api/v1/wishlist/1")
    assert resp.status_code == 401


# --- list ------------------------------------------------------------------


async def test_list_empty(client, user_factory):
    user = user_factory(email="wl-empty@example.com")
    resp = await client.get("/api/v1/wishlist", headers=_headers(user))
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_newest_first(client, user_factory, product_factory):
    user = user_factory(email="wl-order@example.com")
    p1 = product_factory(slug="wl-order-1")
    p2 = product_factory(slug="wl-order-2")
    p3 = product_factory(slug="wl-order-3")
    for p in (p1, p2, p3):
        await client.post("/api/v1/wishlist", json={"product_id": p.id}, headers=_headers(user))
    resp = await client.get("/api/v1/wishlist", headers=_headers(user))
    slugs = [item["product_slug"] for item in resp.json()]
    assert slugs == ["wl-order-3", "wl-order-2", "wl-order-1"]


# --- add -------------------------------------------------------------------


async def test_add_returns_201_with_product_card(client, user_factory, product_factory):
    user = user_factory(email="wl-add@example.com")
    product = product_factory(slug="wl-card", name="Card Product")
    resp = await client.post(
        "/api/v1/wishlist", json={"product_id": product.id}, headers=_headers(user)
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["product_id"] == product.id
    assert body["variant_id"] is None
    assert body["product_slug"] == "wl-card"
    assert body["product_name"] == "Card Product"
    # Wire format is a JSON number (float); the column is a money Decimal.
    assert body["price_usd"] == float(product.price_usd)
    assert body["is_active"] is True
    assert body["in_stock"] is True
    assert body["variant_sku"] is None
    assert "id" in body and "created_at" in body


async def test_add_unknown_product_404(client, user_factory):
    user = user_factory(email="wl-add-404@example.com")
    resp = await client.post(
        "/api/v1/wishlist", json={"product_id": 999999}, headers=_headers(user)
    )
    assert resp.status_code == 404


async def test_add_idempotent_returns_200(client, user_factory, product_factory):
    """An exact re-add returns the existing row with 200, not a duplicate."""
    user = user_factory(email="wl-idem@example.com")
    product = product_factory(slug="wl-idem")
    first = await client.post(
        "/api/v1/wishlist", json={"product_id": product.id}, headers=_headers(user)
    )
    assert first.status_code == 201
    second = await client.post(
        "/api/v1/wishlist", json={"product_id": product.id}, headers=_headers(user)
    )
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    # Only one row in the list.
    listing = (await client.get("/api/v1/wishlist", headers=_headers(user))).json()
    assert len(listing) == 1


# --- variants --------------------------------------------------------------


async def test_add_with_variant_resolves_variant_fields(
    client, user_factory, product_factory, db_session
):
    user = user_factory(email="wl-variant@example.com")
    product = product_factory(slug="wl-variant")
    variant = _make_variant(
        db_session, product, title="Blue / L", sku="VAR-BL-L", price_usd=29.99, stock=3
    )
    resp = await client.post(
        "/api/v1/wishlist",
        json={"product_id": product.id, "variant_id": variant.id},
        headers=_headers(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["variant_id"] == variant.id
    assert body["variant_sku"] == "VAR-BL-L"
    assert body["variant_title"] == "Blue / L"
    # Price resolves to the variant override.
    assert body["price_usd"] == 29.99
    assert body["in_stock"] is True


async def test_add_variant_not_belonging_to_product_404(
    client, user_factory, product_factory, db_session
):
    user = user_factory(email="wl-variant-mismatch@example.com")
    product_a = product_factory(slug="wl-va")
    product_b = product_factory(slug="wl-vb")
    variant_b = _make_variant(db_session, product_b)
    resp = await client.post(
        "/api/v1/wishlist",
        json={"product_id": product_a.id, "variant_id": variant_b.id},
        headers=_headers(user),
    )
    assert resp.status_code == 404


async def test_add_unknown_variant_404(client, user_factory, product_factory):
    user = user_factory(email="wl-variant-404@example.com")
    product = product_factory(slug="wl-variant-404")
    resp = await client.post(
        "/api/v1/wishlist",
        json={"product_id": product.id, "variant_id": 999999},
        headers=_headers(user),
    )
    assert resp.status_code == 404


async def test_same_product_variant_and_variantless_coexist(
    client, user_factory, product_factory, db_session
):
    """A variantless save and a variant save of the same product are distinct rows."""
    user = user_factory(email="wl-coexist@example.com")
    product = product_factory(slug="wl-coexist")
    variant = _make_variant(db_session, product)
    r1 = await client.post(
        "/api/v1/wishlist", json={"product_id": product.id}, headers=_headers(user)
    )
    r2 = await client.post(
        "/api/v1/wishlist",
        json={"product_id": product.id, "variant_id": variant.id},
        headers=_headers(user),
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    listing = (await client.get("/api/v1/wishlist", headers=_headers(user))).json()
    assert len(listing) == 2


# --- delete ----------------------------------------------------------------


async def test_delete_returns_204(client, user_factory, product_factory):
    user = user_factory(email="wl-del@example.com")
    product = product_factory(slug="wl-del")
    created = (
        await client.post(
            "/api/v1/wishlist", json={"product_id": product.id}, headers=_headers(user)
        )
    ).json()
    resp = await client.delete(f"/api/v1/wishlist/{created['id']}", headers=_headers(user))
    assert resp.status_code == 204
    remaining = (await client.get("/api/v1/wishlist", headers=_headers(user))).json()
    assert remaining == []


async def test_delete_unknown_id_404(client, user_factory):
    user = user_factory(email="wl-del-404@example.com")
    resp = await client.delete("/api/v1/wishlist/999999", headers=_headers(user))
    assert resp.status_code == 404


# --- owner gating ----------------------------------------------------------


async def test_cannot_see_other_users_items(client, user_factory, product_factory):
    user_a = user_factory(email="wl-own-a@example.com")
    user_b = user_factory(email="wl-own-b@example.com")
    product = product_factory(slug="wl-own")
    await client.post("/api/v1/wishlist", json={"product_id": product.id}, headers=_headers(user_a))
    resp_b = await client.get("/api/v1/wishlist", headers=_headers(user_b))
    assert resp_b.json() == []


async def test_cannot_delete_other_users_item_404(client, user_factory, product_factory):
    user_a = user_factory(email="wl-x-del-a@example.com")
    user_b = user_factory(email="wl-x-del-b@example.com")
    product = product_factory(slug="wl-x-del")
    created = (
        await client.post(
            "/api/v1/wishlist", json={"product_id": product.id}, headers=_headers(user_a)
        )
    ).json()
    resp = await client.delete(f"/api/v1/wishlist/{created['id']}", headers=_headers(user_b))
    assert resp.status_code == 404
    # A's item still exists.
    a_list = (await client.get("/api/v1/wishlist", headers=_headers(user_a))).json()
    assert len(a_list) == 1
