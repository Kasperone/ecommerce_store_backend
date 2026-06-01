"""Pydantic schemas for admin quote management.

``QuoteStatusLiteral`` is hardcoded (pyright needs literal arguments in type
positions) but cross-checked against the model's ``QUOTE_STATUS_VALUES`` at
import time, so the DB CHECK, the schema, and the route can't drift apart
silently. Mirrors ``CompanyStatusLiteral`` / ``RefundReasonLiteral``.

A quote is a header + nested line items (shape mirrors the order schema). The
**server computes** every ``line_total`` plus the ``subtotal`` / ``total`` — the
client supplies only ``description`` / ``quantity`` / ``unit_price`` /
``product_id?`` per line, never the totals. ``items`` must be non-empty on create
(min_length=1 → 422).

**MVP is manage-only**: there is no "convert to order" schema and no
customer-facing request/negotiation fields — those are deferred. Status
transitions are unconstrained (any → any), so PATCH does not re-validate
against a workflow.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from app.models.quote import QUOTE_STATUS_VALUES
from app.schemas.base import BaseSchema, TimestampSchema

# Hardcoded for pyright; the cross-check below catches drift from the model.
QuoteStatusLiteral = Literal["draft", "sent", "accepted", "rejected", "expired"]

# Import-time drift catch: a status added to one side without the other fails
# the import (and so every dependent route/test) with a clear message.
assert set(QuoteStatusLiteral.__args__) == set(QUOTE_STATUS_VALUES), (  # noqa: S101
    "QuoteStatusLiteral drifted from app.models.quote.QUOTE_STATUS_VALUES: "
    f"schema={set(QuoteStatusLiteral.__args__)} vs model={set(QUOTE_STATUS_VALUES)}"
)

CurrencyLiteral = Literal["USD", "PLN", "EUR"]


class QuoteItemCreate(BaseSchema):
    """A single line on a quote-create / quote-replace body.

    The client supplies the description, quantity, unit price, and an optional
    catalog ``product_id`` link. ``line_total`` is computed server-side, so it is
    intentionally absent here.
    """

    description: Annotated[str, Field(min_length=1, max_length=500)]
    quantity: int = Field(..., gt=0)
    unit_price: float = Field(..., ge=0)
    product_id: int | None = None


class QuoteItemResponse(TimestampSchema):
    """A quote line as the API returns it (includes the computed ``line_total``)."""

    quote_id: int
    product_id: int | None
    description: str
    quantity: int
    unit_price: float
    line_total: float


class QuoteCreate(BaseSchema):
    """Request body for ``POST /api/v1/admin/quotes``.

    ``items`` must be non-empty (min_length=1 → 422 on an empty list). The
    ``company_id`` is validated for existence in the route (404 if missing).
    ``subtotal`` / ``total`` are computed server-side and are not accepted here.
    """

    company_id: int
    currency: CurrencyLiteral = "USD"
    valid_until: datetime | None = None
    notes: str | None = Field(default=None, max_length=5000)
    items: list[QuoteItemCreate] = Field(..., min_length=1)


class QuoteUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/admin/quotes/{id}`` (all fields optional).

    Only fields explicitly sent are applied. ``status`` stays constrained to the
    allowed set by the literal (any → any transition is allowed in MVP). If
    ``items`` is provided (non-empty), the whole line set is replaced and the
    totals recomputed; omit ``items`` to leave the lines untouched.
    """

    status: QuoteStatusLiteral | None = None
    currency: CurrencyLiteral | None = None
    valid_until: datetime | None = None
    notes: str | None = Field(default=None, max_length=5000)
    items: Annotated[list[QuoteItemCreate] | None, Field(min_length=1)] = None


class QuoteResponse(TimestampSchema):
    """A full quote as the API returns it (header + nested ``items``)."""

    company_id: int
    company_name: str | None = None
    status: QuoteStatusLiteral
    currency: str
    valid_until: datetime | None
    notes: str | None
    subtotal: float
    total: float
    items: list[QuoteItemResponse] = Field(default_factory=list)


class QuoteListItem(TimestampSchema):
    """Lightweight quote row for list views (company name + totals, no items)."""

    company_id: int
    company_name: str | None = None
    status: QuoteStatusLiteral
    currency: str
    valid_until: datetime | None
    subtotal: float
    total: float
    item_count: int


class QuoteListResponse(BaseSchema):
    """Paginated quote list."""

    items: list[QuoteListItem]
    total: int
    page: int
    page_size: int
    pages: int
