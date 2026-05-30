"""Pydantic schemas for the refund workflow.

The reason ``Literal`` is hardcoded but cross-checked against the model's
``REFUND_REASON_VALUES`` tuple at import time: a value added to one but not the
other fails the import (so the DB CHECK, the schema, and the route can't drift
apart silently). pyright requires literal arguments in type positions, so we
can't ``Literal[some_tuple_var]`` directly.
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


class RefundCreate(BaseSchema):
    """Body for ``POST /api/v1/admin/orders/{id}/refunds``.

    ``amount`` is currency-scoped to the parent ``Order.currency``; v1 requires
    it to equal ``order.total`` (full refund) — the CRUD raises
    ``PartialRefundUnsupported`` otherwise. ``restock`` defaults False; the admin
    dialog pre-selects True for pre-shipping statuses (D2) but always sends an
    explicit boolean.
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
    """

    order_id: int
    amount: float
    currency: str
    reason: RefundReasonLiteral
    note: str | None
    restock: bool
    provider_refund_id: str | None
    actor_user_id: int | None
    actor_email: str | None = None
    corrective_invoice: CorrectiveInvoiceResponse | None = None
