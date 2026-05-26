"""Integration coverage for ``GET /api/v1/products/`` sorting.

The list endpoint exposes ``sort_by`` (``created_at`` | ``price_usd`` | ``name``)
and ``order`` (``asc`` | ``desc``) as ``Literal`` query params, so out-of-range
values are rejected by FastAPI with 422 before reaching the CRUD layer.
"""

from __future__ import annotations

from datetime import datetime


def _ids(body):
    return [item["id"] for item in body["items"]]


async def test_list_sort_by_price_usd_asc(client, product_factory):
    mid = product_factory(slug="s-mid", price_usd=20.0)
    low = product_factory(slug="s-low", price_usd=10.0)
    high = product_factory(slug="s-high", price_usd=30.0)

    response = await client.get("/api/v1/products/?sort_by=price_usd&order=asc")

    assert response.status_code == 200
    assert _ids(response.json()) == [low.id, mid.id, high.id]


async def test_list_sort_by_name_desc(client, product_factory):
    a = product_factory(name="Alpha", slug="s-alpha")
    c = product_factory(name="Charlie", slug="s-charlie")
    b = product_factory(name="Bravo", slug="s-bravo")

    response = await client.get("/api/v1/products/?sort_by=name&order=desc")

    assert response.status_code == 200
    assert _ids(response.json()) == [c.id, b.id, a.id]


async def test_list_price_asc_nulls_last(client, product_factory):
    low = product_factory(slug="n-low", price_usd=10.0)
    no_usd = product_factory(slug="n-none", price_usd=None, price_pln=50.0)
    high = product_factory(slug="n-high", price_usd=30.0)

    response = await client.get("/api/v1/products/?sort_by=price_usd&order=asc")

    assert response.status_code == 200
    assert _ids(response.json()) == [low.id, high.id, no_usd.id]


async def test_list_default_sort_is_newest_first(client, product_factory):
    oldest = product_factory(slug="d-old", created_at=datetime(2026, 1, 1, 9, 0, 0))
    newest = product_factory(slug="d-new", created_at=datetime(2026, 3, 1, 9, 0, 0))
    middle = product_factory(slug="d-mid", created_at=datetime(2026, 2, 1, 9, 0, 0))

    response = await client.get("/api/v1/products/")

    assert response.status_code == 200
    assert _ids(response.json()) == [newest.id, middle.id, oldest.id]


async def test_list_invalid_sort_by_returns_422(client):
    response = await client.get("/api/v1/products/?sort_by=bogus")
    assert response.status_code == 422


async def test_list_invalid_order_returns_422(client):
    response = await client.get("/api/v1/products/?order=sideways")
    assert response.status_code == 422
