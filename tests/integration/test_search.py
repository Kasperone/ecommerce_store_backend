"""HTTP-level acceptance for the search endpoints (``/api/v1/search``).

The 503 path runs against the real (unconfigured) ``get_meili_client``; the
happy paths override that dependency with a mocked Meili client, so no engine is
needed in CI. Live engine behaviour is verified separately (gated on
``MEILISEARCH_TEST_URL`` / live walkthrough).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core.search import get_meili_client
from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture
def mock_meili_client():
    """Override the search dependency with a mock client (bypasses the 503 gate)."""
    meili = MagicMock()
    app.dependency_overrides[get_meili_client] = lambda: meili
    yield meili
    app.dependency_overrides.pop(get_meili_client, None)


async def test_search_503_when_unconfigured(client):
    # No override → get_meili_client 503s in the test env (Meili unset).
    response = await client.get("/api/v1/search?q=phone")
    assert response.status_code == 503
    assert "Search not configured" in response.json()["detail"]


async def test_suggest_503_when_unconfigured(client):
    response = await client.get("/api/v1/search/suggest?q=phone")
    assert response.status_code == 503


async def test_search_maps_results_and_enriches_facets(client, mock_meili_client, category_factory):
    category = category_factory(name="Electronics")
    mock_meili_client.index.return_value.search.return_value = {
        "hits": [
            {
                "id": 7,
                "slug": "phone",
                "name": "Phone",
                "name_pl": "Telefon",
                "price_usd": 99.0,
                "in_stock": True,
                "category_id": category.id,
                "_formatted": {"name": "<em>Pho</em>ne"},
            }
        ],
        "totalHits": 1,
        "totalPages": 1,
        "page": 1,
        "facetDistribution": {"category_id": {str(category.id): 1}},
        "processingTimeMs": 3,
    }

    response = await client.get("/api/v1/search?q=phone&locale=en&currency=USD")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["slug"] == "phone"
    assert body["items"][0]["name_highlight"] == "<em>Pho</em>ne"
    # Facet count enriched with the DB category name.
    facet = body["facets"]["category_id"][0]
    assert facet == {"value": str(category.id), "label": "Electronics", "count": 1}
    # Page-based pagination + category & tag facets requested.
    _q, opts = mock_meili_client.index.return_value.search.call_args.args
    assert opts["page"] == 1
    assert opts["facets"] == ["category_id", "tags"]


async def test_search_passes_filters_and_sort(client, mock_meili_client):
    mock_meili_client.index.return_value.search.return_value = {
        "hits": [],
        "totalHits": 0,
        "totalPages": 1,
        "page": 1,
        "facetDistribution": {},
        "processingTimeMs": 1,
    }

    response = await client.get(
        "/api/v1/search?q=x&currency=PLN&category_id=3&in_stock=true&sort=price_desc&min_price=10"
    )

    assert response.status_code == 200
    _q, opts = mock_meili_client.index.return_value.search.call_args.args
    assert "category_id = 3" in opts["filter"]
    assert "in_stock = true" in opts["filter"]
    assert "price_pln >= 10.0" in opts["filter"]
    assert opts["sort"] == ["price_pln:desc"]


async def test_suggest_returns_products_and_categories(client, mock_meili_client):
    mock_meili_client.multi_search.return_value = {
        "results": [
            {
                "indexUid": "products",
                "hits": [
                    {
                        "id": 7,
                        "slug": "phone",
                        "name": "Phone",
                        "price_usd": 99.0,
                        "_formatted": {"name": "<em>Pho</em>ne"},
                    }
                ],
            },
            {
                "indexUid": "categories",
                "hits": [{"id": 3, "slug": "electronics", "name": "Electronics"}],
            },
        ]
    }

    response = await client.get("/api/v1/search/suggest?q=ph&locale=en")

    assert response.status_code == 200
    body = response.json()
    assert body["products"][0]["slug"] == "phone"
    assert body["products"][0]["name_highlight"] == "<em>Pho</em>ne"
    assert body["categories"][0]["slug"] == "electronics"


async def test_suggest_requires_query(client, mock_meili_client):
    # q is required + min_length=1 → empty q is a 422 before touching Meili.
    response = await client.get("/api/v1/search/suggest?q=")
    assert response.status_code == 422
