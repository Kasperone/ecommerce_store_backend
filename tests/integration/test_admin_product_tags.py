"""Integration tests for the admin product-tag vocabulary API
(/api/v1/admin/product-tags).

Covers authz, create (+ case-insensitive duplicate 409 + color validation),
list-with-counts, rename/recolor (+ collision 409), and delete-cascade. Mirrors
``test_admin_tags.py`` (customer tags) but exercises the strictly-distinct
``product_tag`` system. Plan: PR-B1 of
``ecommerce_store/.claude/notes/plans/shopify-gap/pending/2026-06-08-product-tags-collections.md``.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.user import UserRole

pytestmark = pytest.mark.integration

TAGS = "/api/v1/admin/product-tags/"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


async def _create_product(client, admin, *, slug: str, tags: list[str] | None = None) -> dict:
    body = {"name": f"Product {slug}", "slug": slug, "price_usd": 10.0}
    if tags is not None:
        body["tags"] = tags
    resp = await client.post("/api/v1/products/", json=body, headers=_headers(admin))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_product_tags_require_admin(client, user_factory):
    customer = user_factory(email="ptag-authz@example.com", role=UserRole.CUSTOMER)
    assert (await client.get(TAGS)).status_code == 401
    assert (await client.get(TAGS, headers=_headers(customer))).status_code == 403
    assert (
        await client.post(TAGS, json={"name": "x"}, headers=_headers(customer))
    ).status_code == 403


async def test_create_and_list_with_counts(client, user_factory):
    admin = user_factory(email="ptag-admin@example.com", role=UserRole.ADMIN)
    a = await client.post(TAGS, json={"name": "Sale", "color": "#3b82f6"}, headers=_headers(admin))
    assert a.status_code == 201
    assert a.json()["name"] == "Sale"
    assert a.json()["color"] == "#3b82f6"
    assert a.json()["usage_count"] == 0
    await client.post(TAGS, json={"name": "clearance"}, headers=_headers(admin))

    listed = await client.get(TAGS, headers=_headers(admin))
    names = [t["name"] for t in listed.json()]
    assert names == ["clearance", "Sale"]  # ordered by lower(name)


async def test_create_duplicate_ci_409(client, user_factory):
    admin = user_factory(email="ptag-dup@example.com", role=UserRole.ADMIN)
    assert (
        await client.post(TAGS, json={"name": "Featured"}, headers=_headers(admin))
    ).status_code == 201
    dup = await client.post(TAGS, json={"name": "  featured "}, headers=_headers(admin))
    assert dup.status_code == 409


async def test_create_invalid_color_422(client, user_factory):
    admin = user_factory(email="ptag-color@example.com", role=UserRole.ADMIN)
    resp = await client.post(TAGS, json={"name": "x", "color": "red"}, headers=_headers(admin))
    assert resp.status_code == 422


async def test_whitespace_only_name_422(client, user_factory):
    admin = user_factory(email="ptag-ws@example.com", role=UserRole.ADMIN)
    resp = await client.post(TAGS, json={"name": "   "}, headers=_headers(admin))
    assert resp.status_code == 422


async def test_rename_and_recolor(client, user_factory):
    admin = user_factory(email="ptag-edit@example.com", role=UserRole.ADMIN)
    tag_id = (await client.post(TAGS, json={"name": "old"}, headers=_headers(admin))).json()["id"]
    resp = await client.patch(
        f"/api/v1/admin/product-tags/{tag_id}",
        json={"name": "new name", "color": "#10b981"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "new name"
    assert resp.json()["color"] == "#10b981"


async def test_rename_collision_409_but_self_ok(client, user_factory):
    admin = user_factory(email="ptag-clash@example.com", role=UserRole.ADMIN)
    await client.post(TAGS, json={"name": "alpha"}, headers=_headers(admin))
    beta_id = (await client.post(TAGS, json={"name": "beta"}, headers=_headers(admin))).json()["id"]

    clash = await client.patch(
        f"/api/v1/admin/product-tags/{beta_id}", json={"name": "ALPHA"}, headers=_headers(admin)
    )
    assert clash.status_code == 409

    self_ok = await client.patch(
        f"/api/v1/admin/product-tags/{beta_id}", json={"name": "BETA"}, headers=_headers(admin)
    )
    assert self_ok.status_code == 200
    assert self_ok.json()["name"] == "BETA"


async def test_update_delete_unknown_404(client, user_factory):
    admin = user_factory(email="ptag-404@example.com", role=UserRole.ADMIN)
    assert (
        await client.patch(
            "/api/v1/admin/product-tags/999999", json={"name": "x"}, headers=_headers(admin)
        )
    ).status_code == 404
    assert (
        await client.delete("/api/v1/admin/product-tags/999999", headers=_headers(admin))
    ).status_code == 404


async def test_usage_count_reflects_assigned_products(client, user_factory):
    admin = user_factory(email="ptag-usage@example.com", role=UserRole.ADMIN)
    await _create_product(client, admin, slug="usage-a", tags=["popular"])
    await _create_product(client, admin, slug="usage-b", tags=["popular"])

    listed = await client.get(TAGS, headers=_headers(admin))
    popular = next(t for t in listed.json() if t["name"] == "popular")
    assert popular["usage_count"] == 2


async def test_delete_cascades_to_products(client, user_factory):
    admin = user_factory(email="ptag-del@example.com", role=UserRole.ADMIN)
    product = await _create_product(client, admin, slug="del-cascade", tags=["temp"])
    listed = await client.get(TAGS, headers=_headers(admin))
    tag_id = next(t for t in listed.json() if t["name"] == "temp")["id"]

    assert (
        await client.delete(f"/api/v1/admin/product-tags/{tag_id}", headers=_headers(admin))
    ).status_code == 204

    # Product survives; its tag list is now empty.
    detail = await client.get(f"/api/v1/products/{product['id']}")
    assert detail.status_code == 200
    assert detail.json()["tags"] == []
