"""Pydantic schemas for admin B2B price-list management.

A price list is a header (name / active flag / notes) plus a set of
``PriceListEntry`` rows, each pinning a per-currency negotiated price for a
product (or a specific variant). The per-resource admin CRUD shape mirrors the
coupons / companies verticals; the entry shape mirrors ``ProductBase``'s
multi-currency price fields.

Prices are ``float`` on the wire (the JSON contract stays a number, matching
``ProductResponse``); the DB stores ``Numeric(12,2)`` / ``Decimal`` and the CRUD
layer quantizes. At least one of the three currency prices must be set on an
entry (mirrors the product at-least-one-price rule) — enforced here (422) and by
the DB CHECK.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.schemas.base import BaseSchema, TimestampSchema

CurrencyLiteral = Literal["USD", "PLN", "EUR"]


class PriceListEntryBase(BaseSchema):
    """Shared entry fields: a product/variant grain + per-currency prices.

    ``variant_id`` NULL = product-grain entry; set = variant-grain entry (wins
    over product-grain for that variant). At least one price must be non-null.
    """

    product_id: int
    variant_id: int | None = None
    price_usd: Annotated[float | None, Field(gt=0)] = None
    price_pln: Annotated[float | None, Field(gt=0)] = None
    price_eur: Annotated[float | None, Field(gt=0)] = None

    @model_validator(mode="after")
    def _at_least_one_price(self) -> PriceListEntryBase:
        if self.price_usd is None and self.price_pln is None and self.price_eur is None:
            raise ValueError("At least one of price_usd / price_pln / price_eur must be set")
        return self


class PriceListEntryCreate(PriceListEntryBase):
    """Request body for adding/replacing an entry on a price list."""


class PriceListEntryUpdate(BaseSchema):
    """Request body for ``PATCH`` of a single entry (all fields optional).

    Only fields explicitly sent are applied. Sending all three prices as null is
    rejected in the route (an entry must keep at least one price)."""

    price_usd: Annotated[float | None, Field(gt=0)] = None
    price_pln: Annotated[float | None, Field(gt=0)] = None
    price_eur: Annotated[float | None, Field(gt=0)] = None


class PriceListEntryResponse(TimestampSchema):
    """A price-list entry as the API returns it.

    ``product_name`` / ``variant_title`` are denormalized for the admin editor so
    a row renders without a per-row product lookup."""

    price_list_id: int
    product_id: int
    variant_id: int | None
    product_name: str | None = None
    variant_title: str | None = None
    price_usd: float | None
    price_pln: float | None
    price_eur: float | None


class PriceListBase(BaseSchema):
    """Shared price-list header fields."""

    name: Annotated[str, Field(min_length=1, max_length=255)]
    is_active: bool = True
    notes: str | None = Field(default=None, max_length=5000)


class PriceListCreate(PriceListBase):
    """Request body for ``POST /api/v1/admin/price-lists``.

    Entries are managed via the dedicated entry endpoints after creation; the
    list is created empty."""


class PriceListUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/admin/price-lists/{id}`` (all optional)."""

    name: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    is_active: bool | None = None
    notes: str | None = Field(default=None, max_length=5000)


class PriceListResponse(TimestampSchema):
    """A full price list as the API returns it (header + nested ``entries``)."""

    name: str
    is_active: bool
    notes: str | None
    entries: list[PriceListEntryResponse] = Field(default_factory=list)


class PriceListListItem(TimestampSchema):
    """Lightweight price-list row for list views (no entries)."""

    name: str
    is_active: bool
    notes: str | None
    entry_count: int


class PriceListListResponse(BaseSchema):
    """Paginated price-list list."""

    items: list[PriceListListItem]
    total: int
    page: int
    page_size: int
    pages: int
