"""Product merchant + SEO meta fields (SEO blueprint D11).

Covers the new columns end-to-end: admin create/update with
``brand``/``gtin``/``mpn``/``condition`` + per-locale ``meta_title``/
``meta_description``, public read exposure, schema-level length/vocabulary
validation (matching the DB CHECKs), and defaults for legacy payloads.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


async def test_create_product_with_merchant_and_seo_fields(client, user_factory, category_factory):
    admin = user_factory(email="mf-create-admin@example.com", role=UserRole.ADMIN)
    cat = category_factory()

    resp = await client.post(
        "/api/v1/products/",
        json={
            "name": "Merchant Fields Product",
            "slug": "mf-full",
            "price_usd": 10.0,
            "category_id": cat.id,
            "brand": "Acme",
            "gtin": "00012345600012",
            "mpn": "ACM-123",
            "condition": "refurbished",
            "meta_title": "Acme widget — buy online",
            "meta_description": "The best Acme widget.",
            "meta_title_pl": "Widżet Acme — kup online",
            "meta_description_pl": "Najlepszy widżet Acme.",
        },
        headers=_headers(admin),
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["brand"] == "Acme"
    assert body["gtin"] == "00012345600012"
    assert body["mpn"] == "ACM-123"
    assert body["condition"] == "refurbished"
    assert body["meta_title"] == "Acme widget — buy online"
    assert body["meta_title_pl"] == "Widżet Acme — kup online"

    # Public read surfaces the new fields too.
    public = await client.get("/api/v1/products/slug/mf-full")
    assert public.status_code == 200
    assert public.json()["brand"] == "Acme"
    assert public.json()["condition"] == "refurbished"


async def test_create_product_defaults_condition_new(client, user_factory, category_factory):
    admin = user_factory(email="mf-default-admin@example.com", role=UserRole.ADMIN)
    cat = category_factory()

    resp = await client.post(
        "/api/v1/products/",
        json={
            "name": "Legacy Payload",
            "slug": "mf-legacy",
            "price_usd": 5.0,
            "category_id": cat.id,
        },
        headers=_headers(admin),
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["condition"] == "new"
    assert body["brand"] is None
    assert body["meta_title"] is None


async def test_update_product_merchant_fields(client, user_factory, product_factory):
    admin = user_factory(email="mf-update-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(slug="mf-update")

    resp = await client.put(
        f"/api/v1/products/{product.id}",
        json={"brand": "Updated Brand", "condition": "used", "meta_title": "New title"},
        headers=_headers(admin),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["brand"] == "Updated Brand"
    assert body["condition"] == "used"
    assert body["meta_title"] == "New title"


async def test_meta_title_over_70_chars_is_422(client, user_factory, category_factory):
    admin = user_factory(email="mf-len-admin@example.com", role=UserRole.ADMIN)
    cat = category_factory()

    resp = await client.post(
        "/api/v1/products/",
        json={
            "name": "Too Long Meta",
            "slug": "mf-too-long",
            "price_usd": 5.0,
            "category_id": cat.id,
            "meta_title": "x" * 71,
        },
        headers=_headers(admin),
    )

    assert resp.status_code == 422


async def test_invalid_condition_is_422(client, user_factory, category_factory):
    admin = user_factory(email="mf-cond-admin@example.com", role=UserRole.ADMIN)
    cat = category_factory()

    resp = await client.post(
        "/api/v1/products/",
        json={
            "name": "Bad Condition",
            "slug": "mf-bad-cond",
            "price_usd": 5.0,
            "category_id": cat.id,
            "condition": "broken",
        },
        headers=_headers(admin),
    )

    assert resp.status_code == 422
