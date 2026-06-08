"""Product-tag assignment on the product writes + the public ``?tag=`` filter.

Covers the get-or-create reconcile on ``POST``/``PUT /api/v1/products`` (the
free-form ``tags`` field), the ``tags`` chips on product responses, and the
case-insensitive ``?tag=`` filter on the public product list. PR-B1.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


async def _admin(user_factory, email: str):
    return user_factory(email=email, role=UserRole.ADMIN)


async def test_create_with_tags_reconciles_and_returns_chips(client, user_factory):
    admin = await _admin(user_factory, "ptags-create@example.com")
    resp = await client.post(
        "/api/v1/products/",
        json={
            "name": "Tagged",
            "slug": "tagged-create",
            "price_usd": 10.0,
            "tags": ["Sale", "  new-arrival ", "sale"],  # dup CI + whitespace
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    names = sorted(t["name"] for t in resp.json()["tags"])
    # Whitespace collapsed, case-insensitive dedupe → 2 distinct tags.
    assert names == ["Sale", "new-arrival"]


async def test_create_reuses_existing_tag_vocabulary(client, user_factory):
    admin = await _admin(user_factory, "ptags-reuse@example.com")
    # Seed the vocabulary via the admin endpoint.
    seeded = await client.post(
        "/api/v1/admin/product-tags/",
        json={"name": "Premium", "color": "#ff0000"},
        headers=_headers(admin),
    )
    seeded_id = seeded.json()["id"]

    resp = await client.post(
        "/api/v1/products/",
        json={"name": "P", "slug": "reuse-tag", "price_usd": 5.0, "tags": ["premium"]},
        headers=_headers(admin),
    )
    assert resp.status_code == 201
    chips = resp.json()["tags"]
    assert len(chips) == 1
    # Same vocabulary row reused (id + color preserved), not a new tag.
    assert chips[0]["id"] == seeded_id
    assert chips[0]["color"] == "#ff0000"


async def test_update_replaces_tags_and_empty_clears(client, user_factory):
    admin = await _admin(user_factory, "ptags-update@example.com")
    created = await client.post(
        "/api/v1/products/",
        json={"name": "U", "slug": "update-tags", "price_usd": 5.0, "tags": ["a", "b"]},
        headers=_headers(admin),
    )
    pid = created.json()["id"]

    # Replace the set.
    replaced = await client.put(
        f"/api/v1/products/{pid}",
        json={"tags": ["c"]},
        headers=_headers(admin),
    )
    assert replaced.status_code == 200
    assert sorted(t["name"] for t in replaced.json()["tags"]) == ["c"]

    # Empty list clears all tags.
    cleared = await client.put(
        f"/api/v1/products/{pid}", json={"tags": []}, headers=_headers(admin)
    )
    assert cleared.status_code == 200
    assert cleared.json()["tags"] == []


async def test_update_without_tags_leaves_them_unchanged(client, user_factory):
    admin = await _admin(user_factory, "ptags-noop@example.com")
    created = await client.post(
        "/api/v1/products/",
        json={"name": "N", "slug": "noop-tags", "price_usd": 5.0, "tags": ["keep"]},
        headers=_headers(admin),
    )
    pid = created.json()["id"]

    # A PUT that omits ``tags`` must not touch them.
    updated = await client.put(
        f"/api/v1/products/{pid}", json={"name": "N2"}, headers=_headers(admin)
    )
    assert updated.status_code == 200
    assert [t["name"] for t in updated.json()["tags"]] == ["keep"]


async def test_public_tag_filter_case_insensitive(client, user_factory):
    admin = await _admin(user_factory, "ptags-filter@example.com")
    await client.post(
        "/api/v1/products/",
        json={"name": "Has", "slug": "filter-has", "price_usd": 5.0, "tags": ["Eco"]},
        headers=_headers(admin),
    )
    await client.post(
        "/api/v1/products/",
        json={"name": "Hasnt", "slug": "filter-hasnt", "price_usd": 5.0},
        headers=_headers(admin),
    )

    resp = await client.get("/api/v1/products/", params={"tag": "eco"})
    assert resp.status_code == 200
    slugs = [p["slug"] for p in resp.json()["items"]]
    assert slugs == ["filter-has"]
    assert resp.json()["total"] == 1


async def test_public_tag_filter_unknown_tag_empty(client, user_factory):
    admin = await _admin(user_factory, "ptags-empty@example.com")
    await client.post(
        "/api/v1/products/",
        json={"name": "Only", "slug": "filter-only", "price_usd": 5.0, "tags": ["x"]},
        headers=_headers(admin),
    )
    resp = await client.get("/api/v1/products/", params={"tag": "does-not-exist"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    assert resp.json()["items"] == []
