"""Storefront search endpoints (backend-proxied Meilisearch).

The browser never talks to Meilisearch directly — it hits these endpoints,
which 503 when the engine is unconfigured (``get_meili_client``). The blocking
sync Meilisearch client is called via ``run_in_threadpool`` (same pattern as the
Stripe SDK) so it never blocks the event loop.

Filter values are typed (int/float/bool/``Literal``), never the raw query
string, so there is no filter-injection surface. The free-text ``q`` goes to
Meili's ``q`` parameter, which the engine handles safely.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.dependencies import get_db
from app.core.search import CATEGORIES_INDEX, PRODUCTS_INDEX, get_meili_client
from app.models.category import Category
from app.models.collection import Collection
from app.schemas.search import (
    FacetBucket,
    SearchResponse,
    SearchResultItem,
    SuggestCategoryItem,
    SuggestProductItem,
    SuggestResponse,
)

router = APIRouter()

Locale = Literal["en", "pl"]
Currency = Literal["USD", "PLN", "EUR"]
SortOption = Literal["relevance", "price_asc", "price_desc", "name"]

HIGHLIGHT_PRE = "<em>"
HIGHLIGHT_POST = "</em>"


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested directly).
# ---------------------------------------------------------------------------


def _price_field(currency: str) -> str:
    return f"price_{currency.lower()}"


def _localized(data: dict, field: str, locale: str) -> str | None:
    """Pick the locale-appropriate value, falling back to the EN field."""
    if locale == "pl":
        return data.get(f"{field}_pl") or data.get(field)
    return data.get(field)


def _quote(value: str) -> str:
    """Quote a string for a Meili filter expression (escape ``\\`` and ``"``).

    The free-text query is handled by Meili's ``q`` param; a *filter* value must
    be a quoted literal. Escaping keeps the typed tag value from breaking out of
    the quoted string — no filter-injection surface.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_filters(
    category_id: int | None,
    min_price: float | None,
    max_price: float | None,
    in_stock: bool | None,
    currency: str,
    tag: str | None = None,
    collection: str | None = None,
) -> list[str]:
    """Build Meili filter expressions. All inputs are typed — no injection."""
    filters: list[str] = []
    if category_id is not None:
        filters.append(f"category_id = {category_id}")
    if in_stock is not None:
        filters.append(f"in_stock = {'true' if in_stock else 'false'}")
    if tag:
        filters.append(f"tags = {_quote(tag)}")
    if collection:
        filters.append(f"collection_slugs = {_quote(collection)}")
    price_field = _price_field(currency)
    if min_price is not None:
        filters.append(f"{price_field} >= {min_price}")
    if max_price is not None:
        filters.append(f"{price_field} <= {max_price}")
    return filters


def build_sort(sort: str, currency: str) -> list[str] | None:
    """Map the sort option to a Meili sort spec (None = relevance ranking)."""
    price_field = _price_field(currency)
    return {
        "relevance": None,
        "price_asc": [f"{price_field}:asc"],
        "price_desc": [f"{price_field}:desc"],
        "name": ["name:asc"],
    }[sort]


def map_product_hit(hit: dict, locale: str) -> SearchResultItem:
    formatted = hit.get("_formatted") or {}
    name = _localized(hit, "name", locale) or ""
    return SearchResultItem(
        id=hit["id"],
        slug=hit["slug"],
        name=name,
        name_highlight=_localized(formatted, "name", locale) or name,
        description=_localized(hit, "description", locale),
        sku=hit.get("sku"),
        image_url=hit.get("image_url"),
        in_stock=bool(hit.get("in_stock", False)),
        category_id=hit.get("category_id"),
        category_name=_localized(hit, "category_name", locale),
        price_usd=hit.get("price_usd"),
        price_pln=hit.get("price_pln"),
        price_eur=hit.get("price_eur"),
    )


def map_suggest_product(hit: dict, locale: str) -> SuggestProductItem:
    formatted = hit.get("_formatted") or {}
    name = _localized(hit, "name", locale) or ""
    return SuggestProductItem(
        id=hit["id"],
        slug=hit["slug"],
        name=name,
        name_highlight=_localized(formatted, "name", locale) or name,
        image_url=hit.get("image_url"),
        in_stock=bool(hit.get("in_stock", False)),
        price_usd=hit.get("price_usd"),
        price_pln=hit.get("price_pln"),
        price_eur=hit.get("price_eur"),
    )


def map_suggest_category(hit: dict, locale: str) -> SuggestCategoryItem:
    return SuggestCategoryItem(
        id=hit["id"],
        slug=hit["slug"],
        name=_localized(hit, "name", locale) or "",
    )


def _build_category_facets(
    db: Session, facet_distribution: dict, locale: str
) -> dict[str, list[FacetBucket]]:
    """Turn Meili's ``{category_id: {"3": 5}}`` distribution into labeled buckets.

    Labels are resolved from the DB (localized) so the frontend facet sidebar
    can render names without a second round-trip.
    """
    counts: dict[str, int] = facet_distribution.get("category_id", {})
    if not counts:
        return {}
    ids = [int(value) for value in counts]
    rows = db.execute(
        select(Category.id, Category.name, Category.name_pl).where(Category.id.in_(ids))
    ).all()
    labels = {row.id: (row.name_pl or row.name) if locale == "pl" else row.name for row in rows}
    buckets = [
        FacetBucket(value=str(cid), label=labels.get(int(cid)), count=count)
        for cid, count in counts.items()
    ]
    buckets.sort(key=lambda bucket: bucket.count, reverse=True)
    return {"category_id": buckets}


def _build_tag_facets(facet_distribution: dict) -> dict[str, list[FacetBucket]]:
    """Turn Meili's ``{tags: {"sale": 5}}`` distribution into labeled buckets.

    Tag names are single-locale merchandising labels — the value *is* the label,
    so no DB round-trip is needed.
    """
    counts: dict[str, int] = facet_distribution.get("tags", {})
    if not counts:
        return {}
    buckets = [FacetBucket(value=name, label=name, count=count) for name, count in counts.items()]
    buckets.sort(key=lambda bucket: bucket.count, reverse=True)
    return {"tags": buckets}


def _build_collection_facets(
    db: Session, facet_distribution: dict, locale: str
) -> dict[str, list[FacetBucket]]:
    """Turn Meili's ``{collection_slugs: {"slug": 5}}`` distribution into labeled
    buckets, resolving the localized collection name from the DB (like categories).
    """
    counts: dict[str, int] = facet_distribution.get("collection_slugs", {})
    if not counts:
        return {}
    slugs = list(counts)
    rows = db.execute(
        select(Collection.slug, Collection.name, Collection.name_pl).where(
            Collection.slug.in_(slugs)
        )
    ).all()
    labels = {row.slug: (row.name_pl or row.name) if locale == "pl" else row.name for row in rows}
    buckets = [
        FacetBucket(value=slug, label=labels.get(slug, slug), count=count)
        for slug, count in counts.items()
    ]
    buckets.sort(key=lambda bucket: bucket.count, reverse=True)
    return {"collection_slugs": buckets}


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------


@router.get("", response_model=SearchResponse)
async def search_products(
    db: Session = Depends(get_db),
    client: Any = Depends(get_meili_client),
    q: str = Query("", max_length=200, description="Search query"),
    locale: Locale = Query("en", description="Locale for localized fields"),
    currency: Currency = Query("USD", description="Currency for price filter/sort"),
    category_id: int | None = Query(None, description="Filter by category"),
    tag: str | None = Query(None, max_length=50, description="Filter by tag name"),
    collection: str | None = Query(None, max_length=100, description="Filter by collection slug"),
    min_price: float | None = Query(None, ge=0, description="Min price (selected currency)"),
    max_price: float | None = Query(None, ge=0, description="Max price (selected currency)"),
    in_stock: bool | None = Query(None, description="Filter by stock availability"),
    sort: SortOption = Query("relevance", description="Sort order"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Faceted product search. 503 when Meilisearch is unconfigured."""
    opts: dict[str, Any] = {
        "page": page,
        "hitsPerPage": page_size,
        "facets": ["category_id", "tags", "collection_slugs"],
        "attributesToHighlight": ["name", "name_pl"],
        "highlightPreTag": HIGHLIGHT_PRE,
        "highlightPostTag": HIGHLIGHT_POST,
    }
    filters = build_filters(category_id, min_price, max_price, in_stock, currency, tag, collection)
    if filters:
        opts["filter"] = filters
    sort_arg = build_sort(sort, currency)
    if sort_arg:
        opts["sort"] = sort_arg

    result = await run_in_threadpool(client.index(PRODUCTS_INDEX).search, q, opts)

    items = [map_product_hit(hit, locale) for hit in result.get("hits", [])]
    facet_distribution = result.get("facetDistribution", {})
    facets = {
        **_build_category_facets(db, facet_distribution, locale),
        **_build_tag_facets(facet_distribution),
        **_build_collection_facets(db, facet_distribution, locale),
    }
    return SearchResponse(
        items=items,
        total=result.get("totalHits", len(items)),
        page=result.get("page", page),
        page_size=page_size,
        pages=result.get("totalPages", 1),
        facets=facets,
        query=q,
        processing_time_ms=result.get("processingTimeMs", 0),
    )


@router.get("/suggest", response_model=SuggestResponse)
async def suggest(
    client: Any = Depends(get_meili_client),
    q: str = Query(..., min_length=1, max_length=200, description="Search query"),
    locale: Locale = Query("en", description="Locale for localized fields"),
    limit: int = Query(5, ge=1, le=10, description="Max hits per section"),
):
    """Mixed autocomplete: product hits + category shortcuts via one multi-search.

    503 when Meilisearch is unconfigured.
    """
    queries = [
        {
            "indexUid": PRODUCTS_INDEX,
            "q": q,
            "limit": limit,
            "attributesToHighlight": ["name", "name_pl"],
            "highlightPreTag": HIGHLIGHT_PRE,
            "highlightPostTag": HIGHLIGHT_POST,
        },
        {"indexUid": CATEGORIES_INDEX, "q": q, "limit": limit},
    ]
    result = await run_in_threadpool(client.multi_search, queries)

    # Match by indexUid rather than position — order-independent.
    hits_by_index = {
        section.get("indexUid"): section.get("hits", []) for section in result.get("results", [])
    }
    return SuggestResponse(
        products=[
            map_suggest_product(hit, locale) for hit in hits_by_index.get(PRODUCTS_INDEX, [])
        ],
        categories=[
            map_suggest_category(hit, locale) for hit in hits_by_index.get(CATEGORIES_INDEX, [])
        ],
        query=q,
    )
