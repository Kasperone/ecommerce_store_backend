"""Integration tests for the public collections API (/api/v1/collections).

Covers the active-collections list (with product counts), slug detail (EN + PL +
404 on miss/inactive), and the ``?collection_id=`` product-list filter ordered by
curation position. Plan: PR-B2 of
``ecommerce_store/.claude/notes/plans/shopify-gap/pending/2026-06-08-product-tags-collections.md``.
"""

from __future__ import annotations

import pytest

from app.models.collection import Collection, collection_products

pytestmark = pytest.mark.integration


def _make_collection(db_session, **overrides) -> Collection:
    defaults = {"name": "Summer", "slug": "summer", "is_active": True, "sort_order": 0}
    defaults.update(overrides)
    collection = Collection(**defaults)
    db_session.add(collection)
    db_session.commit()
    db_session.refresh(collection)
    return collection


def _add_members(db_session, collection_id, ordered_product_ids):
    db_session.execute(
        collection_products.insert(),
        [
            {"collection_id": collection_id, "product_id": pid, "position": index}
            for index, pid in enumerate(ordered_product_ids)
        ],
    )
    db_session.commit()


async def test_list_active_collections_with_counts(client, db_session, product_factory):
    active = _make_collection(db_session, slug="active-col", sort_order=0)
    _make_collection(db_session, name="Hidden", slug="hidden-col", is_active=False)
    product = product_factory(slug="lc-p1")
    _add_members(db_session, active.id, [product.id])

    resp = await client.get("/api/v1/collections/")
    assert resp.status_code == 200
    body = resp.json()
    slugs = [c["slug"] for c in body]
    assert "active-col" in slugs
    assert "hidden-col" not in slugs  # inactive excluded
    by_slug = {c["slug"]: c for c in body}
    assert by_slug["active-col"]["product_count"] == 1


async def test_get_by_slug_en_and_pl(client, db_session):
    _make_collection(db_session, slug="winter", slug_pl="zima")

    en = await client.get("/api/v1/collections/slug/winter")
    assert en.status_code == 200
    assert en.json()["slug"] == "winter"

    pl = await client.get("/api/v1/collections/slug/zima")
    assert pl.status_code == 200
    assert pl.json()["slug"] == "winter"


async def test_get_by_slug_404_on_missing_and_inactive(client, db_session):
    _make_collection(db_session, name="Gone", slug="inactive-col", is_active=False)

    assert (await client.get("/api/v1/collections/slug/nope")).status_code == 404
    assert (await client.get("/api/v1/collections/slug/inactive-col")).status_code == 404


async def test_collection_id_product_filter_ordered_by_position(
    client, db_session, product_factory
):
    collection = _make_collection(db_session, slug="ordered")
    p1 = product_factory(slug="o-p1")
    p2 = product_factory(slug="o-p2")
    p3 = product_factory(slug="o-p3")
    # Curated order: p3, p1, p2 (positions 0,1,2).
    _add_members(db_session, collection.id, [p3.id, p1.id, p2.id])

    resp = await client.get(f"/api/v1/products/?collection_id={collection.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert [item["id"] for item in body["items"]] == [p3.id, p1.id, p2.id]


async def test_collection_id_filter_empty_when_no_members(client, db_session):
    collection = _make_collection(db_session, slug="empty")
    resp = await client.get(f"/api/v1/products/?collection_id={collection.id}")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
