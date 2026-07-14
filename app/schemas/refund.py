"""Pydantic schemas for the refund workflow.

The reason ``Literal`` is hardcoded but cross-checked against the model's
``REFUND_REASON_VALUES`` tuple at import time: a value added to one but not the
other fails the import (so the DB CHECK, the schema, and the route can't drift
apart silently). pyright requires literal arguments in type positions, so we
can't ``Literal[some_tuple_var]`` directly.

v2 adds an **optional per-line picker** on ``RefundCreate.items`` and an
``items: list[RefundItemResponse]`` on ``RefundResponse``. A request omitting
``items`` is still valid (goodwill credit — free-form amount with no line
attribution); when both ``items`` is omitted/empty and ``restock=True``, the
CRUD raises ``RestockRequiresItemsError`` (D5).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.models.refund import REFUND_REASON_VALUES
from app.schemas.base import BaseSchema, TimestampSchema

# Hardcoded for pyright; the cross-check below catches drift from the model.
RefundReasonLiteral = Literal[
    "defective",
    "wrong_item",
    "customer_request",
    "lost_in_transit",
    "damaged_in_transit",
    "other",
]

# Import-time drift catch: a refund reason added to one side without the other
# fails the import (and so every dependent route/test) with a clear message.
assert set(RefundReasonLiteral.__args__) == set(REFUND_REASON_VALUES), (  # noqa: S101
    "RefundReasonLiteral drifted from app.models.refund.REFUND_REASON_VALUES: "
    f"schema={set(RefundReasonLiteral.__args__)} vs model={set(REFUND_REASON_VALUES)}"
)


class RefundItemCreate(BaseSchema):
    """One picked line on a refund request.

    ``order_item_id`` must reference a line on the parent order (validated in
    the CRUD layer — the schema can't enforce cross-resource ownership). ``quantity``
    is units coming back, must be > 0; per-line cumulative qty across all refunds
    for the same line is capped at ``order_item.quantity`` (CRUD raises
    ``RefundLineOverError`` otherwise).
    """

    order_item_id: int
    quantity: int = Field(..., gt=0)


class RefundItemResponse(TimestampSchema):
    """A refunded line on the response.

    ``product_name``/``unit_price``/``line_subtotal`` are denormalised onto the
    response from the joined ``order_item`` row (mirrors the v1 ``actor_email``
    pattern); the FE renders the breakdown without a second round-trip per row.
    ``line_subtotal = quantity * unit_price`` (computed in
    ``_build_refund_item_response``).
    """

    refund_id: int
    order_item_id: int
    quantity: int
    product_name: str | None = None
    unit_price: float | None = None
    line_subtotal: float | None = None


class RefundCreate(BaseSchema):
    """Body for ``POST /api/v1/admin/orders/{id}/refunds``.

    ``amount`` is currency-scoped to the parent ``Order.currency``; v2 lifts the
    full-only guard and validates against the running-total
    (``sum(prior refunds) + amount <= order.total``; CRUD raises
    ``RefundOverError`` otherwise). ``restock`` defaults False; the admin dialog
    pre-selects True for pre-shipping statuses (D2) but always sends an explicit
    boolean. The CRUD additionally rejects ``restock=True`` with no ``items``
    (D5) because there's nothing to put back.

    ``items`` is optional — omitting it (or sending an empty list) records a
    goodwill credit (free-form amount, no line attribution). Provided items get
    a per-line cumulative-qty guard against double-refunding the same line.
    """

    amount: float = Field(..., gt=0, description="Refund amount in the order's currency.")
    reason: RefundReasonLiteral
    note: str | None = Field(
        None,
        max_length=500,
        description="Optional internal note. Never shown to the customer.",
    )
    restock: bool = Field(
        default=False,
        description="Write per-line `order_refunded` inventory movements when True.",
    )
    items: list[RefundItemCreate] | None = Field(
        default=None,
        description=(
            "Optional per-line picker. Omit (or send empty) for a goodwill credit "
            "with no line attribution; required when ``restock=True``."
        ),
    )
    destination: Literal["original", "store_credit"] = Field(
        default="original",
        description=(
            "Where the money goes (D6). ``original`` = record-only (today's "
            "behavior); ``store_credit`` credits the order's user account in the "
            "same transaction (400 ``store_credit_requires_account`` on guest orders)."
        ),
    )


class CorrectiveInvoiceResponse(TimestampSchema):
    """Summary of the issued faktura korygująca that accompanies a refund."""

    kor_number: str
    year: int
    seq: int
    issued_at: datetime
    original_invoice_id: int


class RefundResponse(TimestampSchema):
    """A refund row as the API returns it.

    ``actor_email`` is denormalised onto the response (read from the eager-loaded
    actor relationship by ``_build_refund_response``) so the admin UI doesn't need
    a second round-trip per row. ``note`` is intentionally included on the admin
    side; the customer-facing surface re-shapes the response client-side to drop it
    (privacy — admin notes are internal).

    ``items`` is a (possibly empty) list of picked lines — empty means goodwill
    credit.
    """

    order_id: int
    amount: float
    currency: str
    reason: RefundReasonLiteral
    note: str | None
    restock: bool
    # Where the money went (D6) — derived on the model from the store-credit
    # ledger link (``Refund.destination`` property), not a stored column.
    destination: Literal["original", "store_credit"] = "original"
    provider_refund_id: str | None
    actor_user_id: int | None
    actor_email: str | None = None
    corrective_invoice: CorrectiveInvoiceResponse | None = None
    items: list[RefundItemResponse] = Field(default_factory=list)
