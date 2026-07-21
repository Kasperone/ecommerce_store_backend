"""Pydantic schemas for admin draft-order management.

``DraftOrderStatusLiteral`` is hardcoded (pyright needs literal arguments in
type positions) but cross-checked against the model's
``DRAFT_ORDER_STATUS_VALUES`` at import time, so the DB CHECK, the schema, and
the route can never drift apart silently. Mirrors ``QuoteStatusLiteral``.

A draft order is a header + nested catalog line items. The **server computes**
every ``effective_unit_price`` / ``line_total`` plus the ``subtotal`` /
``total`` — the client supplies only ``product_id`` / ``variant_id?`` /
``quantity`` / ``unit_price_override?`` per line, never the totals.

``currency`` is pinned at create time — ``DraftOrderUpdate`` deliberately has
no currency field. ``status`` is not client-writable either: it flips to
``completed`` only via conversion (PR 3), which locks the draft.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import EmailStr, Field

from app.core.countries import CountryCode
from app.models.draft_order import DRAFT_ORDER_STATUS_VALUES
from app.schemas.base import BaseSchema, TimestampSchema

# Hardcoded for pyright; the cross-check below catches drift from the model.
DraftOrderStatusLiteral = Literal["open", "completed"]

# Import-time drift catch: a status added to one side without the other fails
# the import (and so every dependent route/test) with a clear message.
assert set(DraftOrderStatusLiteral.__args__) == set(DRAFT_ORDER_STATUS_VALUES), (  # noqa: S101
    "DraftOrderStatusLiteral drifted from app.models.draft_order.DRAFT_ORDER_STATUS_VALUES: "
    f"schema={set(DraftOrderStatusLiteral.__args__)} vs model={set(DRAFT_ORDER_STATUS_VALUES)}"
)

CurrencyLiteral = Literal["USD", "PLN", "EUR"]


class DraftOrderCreate(BaseSchema):
    """Request body for ``POST /api/v1/admin/draft-orders``.

    ``currency`` is pinned here and immutable afterwards. The customer is
    optional while drafting — ``user_id`` (validated for existence in the
    route → 404) and/or ``guest_email`` may both be omitted; conversion
    (PR 3) enforces exactly one owner. Lines are added via the item
    sub-endpoints, not here.
    """

    currency: CurrencyLiteral
    user_id: int | None = None
    guest_email: EmailStr | None = None
    notes: str | None = Field(default=None, max_length=5000)


class DraftOrderUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/admin/draft-orders/{id}`` (all optional).

    Only fields explicitly sent are applied; send an explicit ``null`` to
    clear a nullable field (mirrors the ``low_stock_default_threshold``
    nullable-PATCH convention). There is deliberately **no** ``currency``
    (pinned at create) and no ``status`` (set by conversion only). A
    ``completed`` draft rejects every PATCH with 409.
    """

    user_id: int | None = None
    guest_email: EmailStr | None = None
    shipping_address: Annotated[str | None, Field(max_length=500)] = None
    shipping_city: Annotated[str | None, Field(max_length=255)] = None
    shipping_postal_code: Annotated[str | None, Field(max_length=20)] = None
    shipping_country: CountryCode | None = None
    shipping_method_id: int | None = None
    discount: Annotated[float | None, Field(ge=0)] = None
    notes: str | None = Field(default=None, max_length=5000)


class DraftOrderItemCreate(BaseSchema):
    """A single line for ``POST /api/v1/admin/draft-orders/{id}/items``.

    ``variant_id`` follows the order-create rules (``resolve_line_variant``):
    required when the product has active variants, rejected when
    missing/inactive/another product's (422). ``unit_price_override`` NULL
    means "price from the catalog in the draft's currency at read/convert
    time"; a value pins the line to an admin price.
    """

    product_id: int
    variant_id: int | None = None
    quantity: int = Field(..., ge=1)
    unit_price_override: Annotated[float | None, Field(ge=0)] = None


class DraftOrderItemUpdate(BaseSchema):
    """Request body for ``PATCH .../items/{item_id}`` (all fields optional).

    Send an explicit ``null`` for ``unit_price_override`` to clear the pin
    back to live catalog pricing; omit the field to leave it unchanged
    (mirrors the ``low_stock_default_threshold`` nullable-PATCH convention).
    """

    quantity: Annotated[int | None, Field(ge=1)] = None
    unit_price_override: Annotated[float | None, Field(ge=0)] = None


class DraftOrderConvert(BaseSchema):
    """Request body for ``POST /api/v1/admin/draft-orders/{id}/convert``.

    ``payment_method`` lands on the order as-is (free-string column; the
    allowed set here mirrors the admin-conversion MVP — online methods are
    set by the payment-start flow, not here). ``mark_as_paid`` flips the
    order to PAID in the same transaction and issues the gap-free faktura.
    ``send_email`` enqueues the order-confirmation email (plus the paid
    email when ``mark_as_paid``) — works for guests via ``guest_email``.
    """

    payment_method: Literal["manual", "cod"] = "manual"
    mark_as_paid: bool = False
    send_email: bool = False


class DraftOrderItemResponse(TimestampSchema):
    """A draft line as the API returns it.

    ``product_name`` / ``variant_title`` resolve live from the catalog
    relationship (``None`` when the product/variant was deleted — the line is
    visibly stale). ``effective_unit_price`` / ``line_total`` are the
    server-computed preview in the draft's currency (``None`` only on a stale
    line with no override).
    """

    draft_order_id: int
    product_id: int | None
    variant_id: int | None
    product_name: str | None = None
    variant_title: str | None = None
    quantity: int
    unit_price_override: float | None
    effective_unit_price: float | None
    line_total: float | None


class DraftOrderResponse(TimestampSchema):
    """A full draft order as the API returns it (header + nested ``items``)."""

    user_id: int | None
    guest_email: str | None
    customer_email: str | None = None
    status: DraftOrderStatusLiteral
    currency: str
    shipping_address: str | None
    shipping_city: str | None
    shipping_postal_code: str | None
    shipping_country: str | None
    shipping_method_id: int | None
    discount: float
    subtotal: float
    total: float
    notes: str | None
    # Conversion marker: set to the created order's id once the draft is
    # converted (PR 3). ``None`` until then.
    converted_order_id: int | None = None
    items: list[DraftOrderItemResponse] = Field(default_factory=list)


class DraftOrderListItem(TimestampSchema):
    """Lightweight draft row for list views (customer + totals, no items)."""

    user_id: int | None
    guest_email: str | None
    customer_email: str | None = None
    status: DraftOrderStatusLiteral
    currency: str
    discount: float
    subtotal: float
    total: float
    converted_order_id: int | None = None
    item_count: int


class DraftOrderListResponse(BaseSchema):
    """Paginated draft-order list."""

    items: list[DraftOrderListItem]
    total: int
    page: int
    page_size: int
    pages: int
