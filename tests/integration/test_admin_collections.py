"""Integration tests for the admin collections API (/api/v1/admin/collections).

Covers authz gating, CRUD (+ slug / slug_pl collision 400), the hero upload happy
path, membership PUT (ordered replace) + single add/remove, and the audit trail.
Plan: PR-B2 of
``ecommerce_store/.claude/notes/plans/shopify-gap/pending/2026-06-08-product-tags-collections.md``.
"""

from __future__ import annotations

import io
from collections.abc import Iterator

import boto3
import pytest
from botocore.client import BaseClient
from moto import mock_aws
from PIL import Image

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.collection import collection_products
from app.models.user import UserRole

pytestmark = pytest.mark.integration

COLLECTIONS = "/api/v1/admin/collections/"
TEST_BUCKET = "test-bucket"
TEST_PUBLIC_URL = "https://cdn.test.example.com"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _png_bytes(width: int = 100, height: int = 100, color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def r2_mock(monkeypatch) -> Iterator[BaseClient]:
    """Mock R2 + monkeypatch the admin_collections module's inline get_r2_client."""
    monkeypatch.setattr("app.core.config.settings.R2_BUCKET_NAME", TEST_BUCKET)
    monkeypatch.setattr("app.core.config.settings.R2_PUBLIC_URL", TEST_PUBLIC_URL)
    monkeypatch.setattr("app.core.config.settings.R2_ACCOUNT_ID", "test-account")
    monkeypatch.setattr("app.core.config.settings.R2_ACCESS_KEY_ID", "test-key")
    monkeypatch.setattr("app.core.config.settings.R2_SECRET_ACCESS_KEY", "x" * 64)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=TEST_BUCKET)
        monkeypatch.setattr("app.api.v1.admin_collections.get_r2_client", lambda: client)
        yield client


async def _create_collection(client, admin, **overrides) -> dict:
    body = {"name": "Summer Sale", "slug": "summer-sale"}
    body.update(overrides)
    resp = await client.post(COLLECTIONS, json=body, headers=_headers(admin))
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Authz
# ---------------------------------------------------------------------------


async def test_collections_require_admin(client, user_factory):
    customer = user_factory(email="col-authz@example.com", role=UserRole.CUSTOMER)
    assert (await client.get(COLLECTIONS)).status_code == 401
    assert (await client.get(COLLECTIONS, headers=_headers(customer))).status_code == 403
    assert (
        await client.post(COLLECTIONS, json={"name": "x", "slug": "x"}, headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def test_create_lists_and_get(client, user_factory):
    admin = user_factory(email="col-admin@example.com", role=UserRole.ADMIN)
    created = await _create_collection(client, admin, name="Featured", slug="featured")
    assert created["type"] == "manual"
    assert created["is_active"] is True

    listed = await client.get(COLLECTIONS, headers=_headers(admin))
    assert listed.status_code == 200
    slugs = [c["slug"] for c in listed.json()]
    assert "featured" in slugs

    fetched = await client.get(f"{COLLECTIONS}{created['id']}", headers=_headers(admin))
    assert fetched.status_code == 200
    assert fetched.json()["product_count"] == 0


async def test_create_duplicate_slug_400(client, user_factory):
    admin = user_factory(email="col-dup@example.com", role=UserRole.ADMIN)
    await _create_collection(client, admin, slug="dupe")
    dup = await client.post(
        COLLECTIONS, json={"name": "Other", "slug": "dupe"}, headers=_headers(admin)
    )
    assert dup.status_code == 400


async def test_create_duplicate_slug_pl_400(client, user_factory):
    admin = user_factory(email="col-dup-pl@example.com", role=UserRole.ADMIN)
    await _create_collection(client, admin, slug="en-a", slug_pl="pl-a")
    dup = await client.post(
        COLLECTIONS,
        json={"name": "B", "slug": "en-b", "slug_pl": "pl-a"},
        headers=_headers(admin),
    )
    assert dup.status_code == 400


async def test_update_and_slug_pl_collision_400(client, user_factory):
    admin = user_factory(email="col-update@example.com", role=UserRole.ADMIN)
    first = await _create_collection(client, admin, slug="upd-a", slug_pl="upd-pl-a")
    second = await _create_collection(client, admin, name="Two", slug="upd-b")

    ok = await client.put(
        f"{COLLECTIONS}{second['id']}",
        json={"name": "Two Renamed"},
        headers=_headers(admin),
    )
    assert ok.status_code == 200
    assert ok.json()["name"] == "Two Renamed"

    clash = await client.put(
        f"{COLLECTIONS}{second['id']}",
        json={"slug_pl": "upd-pl-a"},
        headers=_headers(admin),
    )
    assert clash.status_code == 400
    assert first["slug_pl"] == "upd-pl-a"


async def test_delete_collection(client, user_factory):
    admin = user_factory(email="col-delete@example.com", role=UserRole.ADMIN)
    created = await _create_collection(client, admin, slug="to-delete")
    resp = await client.delete(f"{COLLECTIONS}{created['id']}", headers=_headers(admin))
    assert resp.status_code == 204
    gone = await client.get(f"{COLLECTIONS}{created['id']}", headers=_headers(admin))
    assert gone.status_code == 404


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


async def test_membership_put_replaces_and_orders(client, user_factory, product_factory):
    admin = user_factory(email="col-members@example.com", role=UserRole.ADMIN)
    collection = await _create_collection(client, admin, slug="members")
    p1 = product_factory(slug="m-p1")
    p2 = product_factory(slug="m-p2")
    p3 = product_factory(slug="m-p3")

    # Set [p2, p1] — order matters.
    resp = await client.put(
        f"{COLLECTIONS}{collection['id']}/products",
        json={"product_ids": [p2.id, p1.id]},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["product_count"] == 2

    # The public product list, filtered by collection, returns them in position order.
    listed = await client.get(f"/api/v1/products/?collection_id={collection['id']}")
    ids = [item["id"] for item in listed.json()["items"]]
    assert ids == [p2.id, p1.id]

    # Replace with [p3] — full replacement.
    resp = await client.put(
        f"{COLLECTIONS}{collection['id']}/products",
        json={"product_ids": [p3.id]},
        headers=_headers(admin),
    )
    assert resp.json()["product_count"] == 1
    listed = await client.get(f"/api/v1/products/?collection_id={collection['id']}")
    assert [item["id"] for item in listed.json()["items"]] == [p3.id]


async def test_membership_put_unknown_product_422(client, user_factory):
    admin = user_factory(email="col-bad-member@example.com", role=UserRole.ADMIN)
    collection = await _create_collection(client, admin, slug="bad-member")
    resp = await client.put(
        f"{COLLECTIONS}{collection['id']}/products",
        json={"product_ids": [999_999]},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_membership_single_add_and_remove(client, user_factory, product_factory):
    admin = user_factory(email="col-addremove@example.com", role=UserRole.ADMIN)
    collection = await _create_collection(client, admin, slug="addremove")
    product = product_factory(slug="ar-p1")

    add = await client.post(
        f"{COLLECTIONS}{collection['id']}/products/{product.id}", headers=_headers(admin)
    )
    assert add.status_code == 201
    assert add.json()["product_count"] == 1

    # Idempotent re-add → still one.
    add2 = await client.post(
        f"{COLLECTIONS}{collection['id']}/products/{product.id}", headers=_headers(admin)
    )
    assert add2.json()["product_count"] == 1

    remove = await client.delete(
        f"{COLLECTIONS}{collection['id']}/products/{product.id}", headers=_headers(admin)
    )
    assert remove.status_code == 204
    fetched = await client.get(f"{COLLECTIONS}{collection['id']}", headers=_headers(admin))
    assert fetched.json()["product_count"] == 0


# ---------------------------------------------------------------------------
# Hero
# ---------------------------------------------------------------------------


async def test_hero_upload_happy_path(client, db_session, user_factory, r2_mock):
    admin = user_factory(email="col-hero@example.com", role=UserRole.ADMIN)
    created = await _create_collection(client, admin, slug="hero")
    resp = await client.post(
        f"{COLLECTIONS}{created['id']}/hero",
        files={"file": ("hero.png", _png_bytes(120, 80), "image/png")},
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["hero_width"] == 120
    assert body["hero_height"] == 80
    assert body["hero_mime"] == "image/png"
    assert body["hero_image_url"].startswith(TEST_PUBLIC_URL + "/collections/")


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def test_audit_trail(client, db_session, user_factory, product_factory):
    admin = user_factory(email="col-audit@example.com", role=UserRole.ADMIN)
    created = await _create_collection(client, admin, slug="audited")
    product = product_factory(slug="audit-p1")
    await client.put(
        f"{COLLECTIONS}{created['id']}/products",
        json={"product_ids": [product.id]},
        headers=_headers(admin),
    )

    events = db_session.query(AdminAuditEvent).all()
    resource_types = {e.resource_type for e in events}
    assert "collection" in resource_types
    assert "collection_product" in resource_types
    # Sanity: the membership row landed.
    count = (
        db_session.query(collection_products)
        .filter(collection_products.c.collection_id == created["id"])
        .count()
    )
    assert count == 1
