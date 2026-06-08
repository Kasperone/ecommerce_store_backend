"""Pydantic schemas for admin customer segments.

``SegmentFilters`` is the **single source of truth** for a segment's predicate:
it validates writes (``extra="forbid"`` — unknown keys are rejected, no eval, no
query language) and drives the set-based SQL resolver in ``app.crud.segment``.

All filter fields are optional and **AND-combined**; an empty filter object
matches every customer. The one cross-field rule: ``min_total_spent`` requires a
``currency`` (this is a per-currency app with no runtime conversion — spend has
no meaning without a currency), enforced by a ``model_validator``.

The CRUD shape (Create / Update / Response / List) mirrors ``campaign.py``.
``SegmentPreview`` returns a resolved count plus a sample of ``CustomerListItem``
rows (the same lightweight admin-customer row the customer list uses).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from app.schemas.base import BaseSchema, TimestampSchema
from app.schemas.customer import CustomerListItem

CurrencyLiteral = Literal["USD", "PLN", "EUR"]
TagMatchLiteral = Literal["any", "all"]
SegmentRoleLiteral = Literal["customer", "b2b"]


class SegmentFilters(BaseSchema):
    """The closed, typed customer predicate that defines a segment.

    Unknown keys are rejected (``extra="forbid"``). Every field is optional;
    set fields are AND-combined by the resolver. An empty object matches all
    customers.
    """

    model_config = ConfigDict(extra="forbid")

    # Customer tags (customer_tags join). ``tag_match="any"`` → has at least one
    # of the tags; ``"all"`` → has every listed tag.
    tag_ids: list[int] | None = None
    tag_match: TagMatchLiteral = "any"

    # Lifetime order count (all statuses, mirrors crud_user.order_metrics).
    min_order_count: int | None = Field(default=None, ge=0)
    max_order_count: int | None = Field(default=None, ge=0)

    # Lifetime spend in a specific currency (excludes cancelled orders). A
    # currency is REQUIRED when min_total_spent is set — no runtime conversion.
    min_total_spent: float | None = Field(default=None, ge=0)
    currency: CurrencyLiteral | None = None

    # Last order date window (inclusive bounds against the most recent order).
    last_order_before: date | None = None
    last_order_after: date | None = None

    # Country of the user's default shipping address (Address.country).
    country: str | None = Field(default=None, max_length=100)

    # Role: "customer" → retail CUSTOMER role; "b2b" → a member of any company.
    role: SegmentRoleLiteral | None = None

    @model_validator(mode="after")
    def _check_currency_required(self) -> SegmentFilters:
        if self.min_total_spent is not None and self.currency is None:
            raise ValueError("currency is required when min_total_spent is set")
        return self


class SegmentBase(BaseSchema):
    """Shared segment fields (create + response)."""

    name: Annotated[str, Field(min_length=1, max_length=100)]
    description: Annotated[str | None, Field(max_length=255)] = None
    filters: SegmentFilters = Field(default_factory=SegmentFilters)
    is_active: bool = True


class SegmentCreate(SegmentBase):
    """Request body for ``POST /api/v1/admin/segments``."""


class SegmentUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/admin/segments/{id}`` (all fields optional)."""

    name: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    description: Annotated[str | None, Field(max_length=255)] = None
    filters: SegmentFilters | None = None
    is_active: bool | None = None


class SegmentResponse(SegmentBase, TimestampSchema):
    """A segment row as the API returns it."""


class SegmentListResponse(BaseSchema):
    """Paginated segment list."""

    items: list[SegmentResponse]
    total: int
    page: int
    page_size: int
    pages: int


class SegmentPreview(BaseSchema):
    """Resolved segment membership preview: total match count + a sample page."""

    count: int
    sample: list[CustomerListItem]
