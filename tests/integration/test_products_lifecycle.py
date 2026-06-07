"""Status-code contract for the public PDP-by-slug endpoint (SEO blueprint D12).

* Active product -> 200 (unchanged).
* Deactivated (``is_active=False``) product -> **410 Gone** so crawlers drop
  the URL fast and delisted items stop being publicly reachable by slug.
* Never-existed slug -> 404 (unchanged).
* Admin/ID-based access is unaffected by the deactivation gate.
"""

from __future__ import annotations


async def test_get_by_slug_active_product_returns_200(client, product_factory):
    product = product_factory(slug="lifecycle-active")

    response = await client.get(f"/api/v1/products/slug/{product.slug}")

    assert response.status_code == 200
    assert response.json()["slug"] == "lifecycle-active"


async def test_get_by_slug_inactive_product_returns_410(client, product_factory):
    product = product_factory(slug="lifecycle-inactive", is_active=False)

    response = await client.get(f"/api/v1/products/slug/{product.slug}")

    assert response.status_code == 410
    assert response.json()["detail"] == "Product no longer available"


async def test_get_by_slug_unknown_slug_returns_404(client):
    response = await client.get("/api/v1/products/slug/never-existed")

    assert response.status_code == 404
    assert response.json()["detail"] == "Product not found"


async def test_get_by_id_inactive_product_still_returns_200(client, product_factory):
    """ID-based access (admin surfaces) is not gated by ``is_active``."""
    product = product_factory(slug="lifecycle-inactive-by-id", is_active=False)

    response = await client.get(f"/api/v1/products/{product.id}")

    assert response.status_code == 200
    assert response.json()["id"] == product.id
