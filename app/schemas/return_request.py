"""Pydantic schemas for the returns (RMA) workflow.

The status/reason ``Literal``s are hardcoded but cross-checked against the
model's VALUES tuples at import time — a value added to one but not the other
fails the import, so the DB CHECK, the schema, and the route can never drift
apart silently (same pattern as ``app/schemas/refund.py``).

Customer surface: ``ReturnRequestCreate`` (line picker + reasons + comment),
``ReturnRequestResponse``, and the ``ReturnEligibilityResponse`` that backs the
storefront "Request return" modal (single server-side source for the window +
per-line returnable quantities). The customer surface drops ``admin_note``
client-side (same convention as the refund ``note``).

Admin surface: paginated list + guarded transition bodies + the
refund-from-return body (which delegates to the existing refund machinery —
``reason`` is the *refund* reason vocabulary, defaulting to
``customer_request``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.models.return_request import RETURN_REASON_VALUES, RETURN_STATUS_VALUES
from app.schemas.base import BaseSchema, TimestampSchema
from app.schemas.refund import RefundReasonLiteral

# Hardcoded for pyright; the cross-checks below catch drift from the model.
ReturnStatusLiteral = Literal["requested", "approved", "rejected", "received", "refunded"]
ReturnReasonLiteral = Literal[
    "defective",
    "wrong_item",
    "not_as_described",
    "no_longer_needed",
    "other",
]

# Import-time drift catches — see app/schemas/refund.py for the rationale.
assert set(ReturnStatusLiteral.__args__) == set(RETURN_STATUS_VALUES), (  # noqa: S101
    "ReturnStatusLiteral drifted from app.models.return_request.RETURN_STATUS_VALUES: "
    f"schema={set(ReturnStatusLiteral.__args__)} vs model={set(RETURN_STATUS_VALUES)}"
)
assert set(ReturnReasonLiteral.__args__) == set(RETURN_REASON_VALUES), (  # noqa: S101
    "ReturnReasonLiteral drifted from app.models.return_request.RETURN_REASON_VALUES: "
    f"schema={set(ReturnReasonLiteral.__args__)} vs model={set(RETURN_REASON_VALUES)}"
)


class ReturnItemCreate(BaseSchema):
    """One picked line on a return request.

    ``order_item_id`` must reference a line on the parent order (validated in
    the CRUD layer). ``quantity`` is capped at the line's *returnable*
    quantity: ``order_item.quantity - Σ prior non-rejected return qty -
    Σ refund qty not linked from any return`` (CRUD raises
    ``ReturnLineOverError`` otherwise).
    """

    order_item_id: int
    quantity: int = Field(..., gt=0)
    reason: ReturnReasonLiteral


class ReturnItemResponse(TimestampSchema):
    """A returned line on the response.

    ``product_name``/``variant_title``/``unit_price``/``line_subtotal`` are
    denormalised from the joined ``order_item`` row (model properties — mirrors
    ``RefundItemResponse``) so the FE renders the breakdown without a second
    round-trip per row.
    """

    return_request_id: int
    order_item_id: int
    quantity: int
    reason: ReturnReasonLiteral
    product_name: str | None = None
    variant_title: str | None = None
    unit_price: float | None = None
    line_subtotal: float | None = None


class ReturnRequestCreate(BaseSchema):
    """Body for ``POST /api/v1/orders/{order_id}/returns`` (customer)."""

    items: list[ReturnItemCreate] = Field(..., min_length=1)
    comment: str | None = Field(
        None,
        max_length=2000,
        description="Optional free-text message from the customer.",
    )


class ReturnRequestResponse(TimestampSchema):
    """A return request as the API returns it.

    ``admin_note`` is included because the admin uses the same shape; the
    customer surface drops it client-side (same convention as the refund
    ``note``). ``order_number``/``currency``/``customer_email`` are
    denormalised model properties (eager-loaded relationships — no N+1).
    """

    order_id: int
    user_id: int | None
    status: ReturnStatusLiteral
    comment: str | None
    admin_note: str | None
    refund_id: int | None
    resolved_at: datetime | None
    order_number: str | None = None
    currency: str | None = None
    customer_email: str | None = None
    items: list[ReturnItemResponse] = Field(default_factory=list)


class ReturnRequestListResponse(BaseSchema):
    """Paginated admin list (standard envelope)."""

    items: list[ReturnRequestResponse]
    total: int
    page: int
    page_size: int
    pages: int


class ReturnEligibilityLine(BaseSchema):
    """Per-line returnable quantity for the storefront return modal."""

    order_item_id: int
    product_name: str
    variant_title: str | None = None
    quantity: int
    returnable_qty: int


class ReturnEligibilityResponse(BaseSchema):
    """``GET /api/v1/orders/{order_id}/returns/eligibility``.

    The single server-side source for the FE modal: ``eligible`` is the
    create-endpoint's verdict (order delivered + returns enabled + window open
    + at least one returnable unit); ``reason`` carries a machine-readable code
    when not eligible (``not_delivered`` / ``returns_disabled`` /
    ``window_closed`` / ``nothing_returnable``). ``window_ends_at`` is
    ``delivered_at`` (legacy fallback: ``updated_at``) + the configured
    ``returns_window_days`` — null when the order was never delivered or
    returns are disabled.
    """

    eligible: bool
    reason: (
        Literal["not_delivered", "returns_disabled", "window_closed", "nothing_returnable"] | None
    ) = None
    window_ends_at: datetime | None = None
    lines: list[ReturnEligibilityLine] = Field(default_factory=list)


class ReturnActionRequest(BaseSchema):
    """Optional body for the admin approve / reject / receive transitions."""

    admin_note: str | None = Field(None, max_length=2000)


class ReturnRefundRequest(BaseSchema):
    """Body for ``POST /api/v1/admin/returns/{id}/refund``.

    Delegates to the existing refund choke point (``crud_refund
    .create_for_order``) with the return's lines as the per-line picker.
    ``amount`` defaults to ``Σ order_item.unit_price x returned qty`` when
    omitted; ``reason`` is the *refund* reason vocabulary (default
    ``customer_request``); ``restock`` writes the variant-aware
    ``order_refunded`` movements at refund time (restock happens **here**, not
    at receive).
    """

    amount: float | None = Field(
        None, gt=0, description="Refund amount in the order's currency; defaults to the line total."
    )
    restock: bool = Field(default=False, description="Restock the returned lines when True.")
    reason: RefundReasonLiteral = "customer_request"
    note: str | None = Field(
        None, max_length=500, description="Optional internal refund note (never shown)."
    )
