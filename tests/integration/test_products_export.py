"""Integration tests for the admin product CSV export.

Covers ``GET /api/v1/admin/products/export`` — admin-gating (401/403), content
type + filename, a seeded product row, packed tags/collection_slugs, the
``has_variants`` flag, and that the list filters (search / category / is_active)
are honored. CSV-BE1 of the Wave-3 product CSV feature (export only).
"""

from __future__ import annotations

import csv
import io

import pytest

from app.core.security import create_access_token
from app.models.collection import Collection, collection_products
from app.models.product_tag import ProductTag
from app.models.user import UserRole

pytestmark = pytest.mark.integration

EXPORT_URL = "/api/v1/admin/products/export"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _parse(content: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))


async def test_export_requires_auth(client):
    resp = await client.get(EXPORT_URL)
    assert resp.status_code == 401


async def test_export_forbidden_for_customer(client, user_factory):
    customer = user_factory(email="prodexp-cust@example.com")
    resp = await client.get(EXPORT_URL, headers=_headers(customer))
    assert resp.status_code == 403


async def test_export_returns_csv_with_seeded_product(client, user_factory, product_factory):
    admin = user_factory(email="prodexp-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(name="Exportable", slug="exportable", sku="EXP-1")

    resp = await client.get(EXPORT_URL, headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith('.csv"')
    # UTF-8 BOM for Excel + Polish diacritics.
    assert resp.content.startswith(b"\xef\xbb\xbf")

    rows = _parse(resp.content)
    match = next(r for r in rows if r["id"] == str(product.id))
    assert match["slug"] == "exportable"
    assert match["sku"] == "EXP-1"
    assert match["name"] == "Exportable"
    assert match["has_variants"] == "false"
    assert match["category_slug"] == product.category.slug


async def test_export_packs_tags_and_collections(client, user_factory, product_factory, db_session):
    admin = user_factory(email="prodexp-tags@example.com", role=UserRole.ADMIN)
    product = product_factory(name="Tagged", slug="tagged")

    tag_a = ProductTag(name="sale")
    tag_b = ProductTag(name="new-arrival")
    product.tags.extend([tag_a, tag_b])
    collection = Collection(name="Summer", slug="summer")
    db_session.add(collection)
    db_session.flush()
    db_session.execute(
        collection_products.insert().values(collection_id=collection.id, product_id=product.id)
    )
    db_session.commit()

    resp = await client.get(EXPORT_URL, headers=_headers(admin))
    assert resp.status_code == 200
    match = next(r for r in _parse(resp.content) if r["id"] == str(product.id))
    # Tags ordered by lower(name) per the relationship order_by.
    assert match["tags"] == "new-arrival; sale"
    assert match["collection_slugs"] == "summer"


async def test_export_honors_search_filter(client, user_factory, product_factory):
    admin = user_factory(email="prodexp-search@example.com", role=UserRole.ADMIN)
    keep = product_factory(name="FindMeWidget", slug="find-me-widget")
    drop = product_factory(name="OtherThing", slug="other-thing")

    resp = await client.get(EXPORT_URL, params={"search": "FindMeWidget"}, headers=_headers(admin))
    assert resp.status_code == 200
    ids = {r["id"] for r in _parse(resp.content)}
    assert str(keep.id) in ids
    assert str(drop.id) not in ids


async def test_export_honors_is_active_filter(client, user_factory, product_factory):
    admin = user_factory(email="prodexp-active@example.com", role=UserRole.ADMIN)
    active = product_factory(name="ActiveOne", slug="active-one", is_active=True)
    inactive = product_factory(name="InactiveOne", slug="inactive-one", is_active=False)

    # is_active omitted → export all (both present).
    resp_all = await client.get(EXPORT_URL, headers=_headers(admin))
    ids_all = {r["id"] for r in _parse(resp_all.content)}
    assert {str(active.id), str(inactive.id)} <= ids_all

    # is_active=true → only the active one.
    resp_active = await client.get(
        EXPORT_URL, params={"is_active": "true"}, headers=_headers(admin)
    )
    ids_active = {r["id"] for r in _parse(resp_active.content)}
    assert str(active.id) in ids_active
    assert str(inactive.id) not in ids_active


async def test_export_honors_category_filter(client, user_factory, product_factory):
    admin = user_factory(email="prodexp-cat@example.com", role=UserRole.ADMIN)
    in_cat = product_factory(name="InCat", slug="in-cat")
    other = product_factory(name="OutCat", slug="out-cat")
    assert in_cat.category_id != other.category_id

    resp = await client.get(
        EXPORT_URL, params={"category_id": in_cat.category_id}, headers=_headers(admin)
    )
    ids = {r["id"] for r in _parse(resp.content)}
    assert str(in_cat.id) in ids
    assert str(other.id) not in ids
