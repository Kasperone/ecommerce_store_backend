"""Integration tests for the admin tag-vocabulary API (/api/v1/admin/tags).

Covers authz, create (+ case-insensitive duplicate 409 + color validation),
list-with-counts, rename/recolor (+ collision 409), and delete-cascades. Plan:
``ecommerce_store/.claude/notes/plans/admin/in-progress/2026-05-27-customer-notes-and-tags.md``.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.user import UserRole

pytestmark = pytest.mark.integration

TAGS = "/api/v1/admin/tags/"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


async def test_tags_require_admin(client, user_factory):
    customer = user_factory(email="tag-authz@example.com", role=UserRole.CUSTOMER)
    assert (await client.get(TAGS)).status_code == 401
    assert (await client.get(TAGS, headers=_headers(customer))).status_code == 403
    assert (
        await client.post(TAGS, json={"name": "x"}, headers=_headers(customer))
    ).status_code == 403


async def test_create_and_list_with_counts(client, user_factory):
    admin = user_factory(email="tag-admin@example.com", role=UserRole.ADMIN)
    a = await client.post(TAGS, json={"name": "VIP", "color": "#3b82f6"}, headers=_headers(admin))
    assert a.status_code == 201
    assert a.json()["name"] == "VIP"
    assert a.json()["color"] == "#3b82f6"
    assert a.json()["usage_count"] == 0
    await client.post(TAGS, json={"name": "wholesale"}, headers=_headers(admin))

    listed = await client.get(TAGS, headers=_headers(admin))
    names = [t["name"] for t in listed.json()]
    assert names == ["VIP", "wholesale"]  # ordered by lower(name)


async def test_create_duplicate_ci_409(client, user_factory):
    admin = user_factory(email="tag-dup@example.com", role=UserRole.ADMIN)
    assert (
        await client.post(TAGS, json={"name": "Gold"}, headers=_headers(admin))
    ).status_code == 201
    dup = await client.post(TAGS, json={"name": "  gold "}, headers=_headers(admin))
    assert dup.status_code == 409


async def test_create_invalid_color_422(client, user_factory):
    admin = user_factory(email="tag-color@example.com", role=UserRole.ADMIN)
    resp = await client.post(TAGS, json={"name": "x", "color": "red"}, headers=_headers(admin))
    assert resp.status_code == 422


async def test_whitespace_only_name_422(client, user_factory):
    admin = user_factory(email="tag-ws@example.com", role=UserRole.ADMIN)
    resp = await client.post(TAGS, json={"name": "   "}, headers=_headers(admin))
    assert resp.status_code == 422


async def test_rename_and_recolor(client, user_factory):
    admin = user_factory(email="tag-edit@example.com", role=UserRole.ADMIN)
    tag_id = (await client.post(TAGS, json={"name": "old"}, headers=_headers(admin))).json()["id"]
    resp = await client.patch(
        f"/api/v1/admin/tags/{tag_id}",
        json={"name": "new name", "color": "#10b981"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "new name"
    assert resp.json()["color"] == "#10b981"


async def test_rename_collision_409_but_self_ok(client, user_factory):
    admin = user_factory(email="tag-clash@example.com", role=UserRole.ADMIN)
    await client.post(TAGS, json={"name": "alpha"}, headers=_headers(admin))
    beta_id = (await client.post(TAGS, json={"name": "beta"}, headers=_headers(admin))).json()["id"]

    clash = await client.patch(
        f"/api/v1/admin/tags/{beta_id}", json={"name": "ALPHA"}, headers=_headers(admin)
    )
    assert clash.status_code == 409

    # Renaming a tag to its own name in different case is not a collision.
    self_ok = await client.patch(
        f"/api/v1/admin/tags/{beta_id}", json={"name": "BETA"}, headers=_headers(admin)
    )
    assert self_ok.status_code == 200
    assert self_ok.json()["name"] == "BETA"


async def test_update_delete_unknown_404(client, user_factory):
    admin = user_factory(email="tag-404@example.com", role=UserRole.ADMIN)
    assert (
        await client.patch("/api/v1/admin/tags/999999", json={"name": "x"}, headers=_headers(admin))
    ).status_code == 404
    assert (
        await client.delete("/api/v1/admin/tags/999999", headers=_headers(admin))
    ).status_code == 404


async def test_delete_cascades_to_customers(client, user_factory):
    admin = user_factory(email="tag-del@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="tag-del-cust@example.com")
    tag_id = (await client.post(TAGS, json={"name": "temp"}, headers=_headers(admin))).json()["id"]
    await client.post(
        f"/api/v1/admin/customers/{customer.id}/tags",
        json={"tag_id": tag_id},
        headers=_headers(admin),
    )

    assert (
        await client.delete(f"/api/v1/admin/tags/{tag_id}", headers=_headers(admin))
    ).status_code == 204
    detail = await client.get(f"/api/v1/admin/customers/{customer.id}", headers=_headers(admin))
    assert detail.json()["tags"] == []
