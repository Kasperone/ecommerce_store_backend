"""Schemas for the storefront search endpoints.

Results carry all three currency prices (like the catalog list) so the frontend
selects + formats with its existing ``money.ts`` helpers; the ``currency`` query
param only drives the price filter/sort field. Name/description/category name
are returned **already localized** per the requested ``locale`` so the highlight
(``*_highlight``) and the plain text stay consistent.
"""

from app.schemas.base import BaseSchema


class SearchResultItem(BaseSchema):
    """One product hit on the faceted results page."""

    id: int
    slug: str
    name: str
    name_highlight: str
    description: str | None = None
    sku: str | None = None
    image_url: str | None = None
    in_stock: bool = False
    category_id: int | None = None
    category_name: str | None = None
    price_usd: float | None = None
    price_pln: float | None = None
    price_eur: float | None = None


class FacetBucket(BaseSchema):
    """One bucket of a facet distribution (value + human label + match count)."""

    value: str
    label: str | None = None
    count: int


class SearchResponse(BaseSchema):
    """Paginated, faceted product search results."""

    items: list[SearchResultItem]
    total: int
    page: int
    page_size: int
    pages: int
    facets: dict[str, list[FacetBucket]]
    query: str
    processing_time_ms: int


class SuggestProductItem(BaseSchema):
    """A product hit in the autocomplete dropdown."""

    id: int
    slug: str
    name: str
    name_highlight: str
    image_url: str | None = None
    in_stock: bool = False
    price_usd: float | None = None
    price_pln: float | None = None
    price_eur: float | None = None


class SuggestCategoryItem(BaseSchema):
    """A category shortcut in the autocomplete dropdown."""

    id: int
    slug: str
    name: str


class SuggestResponse(BaseSchema):
    """Mixed autocomplete results: product hits + category shortcuts."""

    products: list[SuggestProductItem]
    categories: list[SuggestCategoryItem]
    query: str
