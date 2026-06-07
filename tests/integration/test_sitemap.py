"""Public sitemap-source endpoints (SEO blueprint D7).

Contract: unauthenticated, active-only, slim payloads —
``/sitemap/products`` -> ``[{slug, updated_at, images: [url, ...]}]`` and
``/sitemap/categories`` -> ``[{slug, slug_pl, updated_at}]``. Product image
URLs follow ``sort_order``; deactivated rows never appear (they 410 on the
PDP and must not be advertised to crawlers).
"""

from __future__ import annotations

from app.models.product_image import ProductImage


async def test_sitemap_products_returns_active_only(client, product_factory):
    active = product_factory(slug="sm-active")
    product_factory(slug="sm-inactive", is_active=False)

    response = await client.get("/api/v1/sitemap/products")

    assert response.status_code == 200
    body = response.json()
    slugs = [row["slug"] for row in body]
    assert "sm-active" in slugs
    assert "sm-inactive" not in slugs
    row = next(r for r in body if r["slug"] == "sm-active")
    assert set(row) == {"slug", "updated_at", "images"}
    assert row["updated_at"] is not None
    assert row["images"] == []
    assert active.slug == "sm-active"


async def test_sitemap_products_images_follow_sort_order(client, product_factory, db_session):
    product = product_factory(slug="sm-with-images")
    for sort_order, url in (
        (1, "https://cdn.example.com/b.webp"),
        (0, "https://cdn.example.com/a.webp"),
    ):
        db_session.add(
            ProductImage(
                product_id=product.id,
                url=url,
                sort_order=sort_order,
                is_primary=sort_order == 0,
                width=800,
                height=600,
                bytes=1_000,
                mime="image/webp",
            )
        )
    db_session.commit()

    response = await client.get("/api/v1/sitemap/products")

    assert response.status_code == 200
    row = next(r for r in response.json() if r["slug"] == "sm-with-images")
    assert row["images"] == [
        "https://cdn.example.com/a.webp",
        "https://cdn.example.com/b.webp",
    ]


async def test_sitemap_categories_returns_active_only_with_pl_slug(client, category_factory):
    category_factory(slug="sm-cat-en", slug_pl="sm-cat-pl")
    category_factory(slug="sm-cat-hidden", is_active=False)

    response = await client.get("/api/v1/sitemap/categories")

    assert response.status_code == 200
    body = response.json()
    slugs = [row["slug"] for row in body]
    assert "sm-cat-en" in slugs
    assert "sm-cat-hidden" not in slugs
    row = next(r for r in body if r["slug"] == "sm-cat-en")
    assert set(row) == {"slug", "slug_pl", "updated_at"}
    assert row["slug_pl"] == "sm-cat-pl"
    assert row["updated_at"] is not None


async def test_sitemap_endpoints_are_public(client):
    """No auth dependency: both endpoints answer 200 to anonymous callers."""
    for path in ("/api/v1/sitemap/products", "/api/v1/sitemap/categories"):
        response = await client.get(path)
        assert response.status_code == 200
        assert isinstance(response.json(), list)
