"""Pydantic schemas for store credit (admin per-customer + owner's account view).

The summary shape ``{accounts, entries}`` is shared by the admin endpoint
(``GET /admin/customers/{id}/store-credit``) and the owner's
(``GET /account/store-credit``) so the FE renders one component for both.
Money is ``float`` on the wire (house rule; storage/math stay ``Decimal``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator

from app.schemas.base import BaseSchema

# Mirrors ``app.schemas.account`` / ``app.schemas.gift_card``.
CurrencyLiteral = Literal["USD", "PLN", "EUR"]


class StoreCreditAccountResponse(BaseSchema):
    """One per-currency balance."""

    currency: str
    balance: float


class StoreCreditEntryResponse(BaseSchema):
    """One ledger row, labelled with its account's currency."""

    id: int
    currency: str
    amount: float
    kind: str
    balance_after: float
    order_id: int | None = None
    refund_id: int | None = None
    note: str | None = None
    created_at: datetime


class StoreCreditSummaryResponse(BaseSchema):
    """The customer's store credit: per-currency accounts + ledger (newest first)."""

    accounts: list[StoreCreditAccountResponse]
    entries: list[StoreCreditEntryResponse]


class StoreCreditAdjustRequest(BaseSchema):
    """Request body for ``POST /admin/customers/{id}/store-credit/adjust``.

    ``amount`` is signed: positive grants, negative claws back (a result below
    zero is refused with 400 ``store_credit_balance_negative``).
    """

    amount: float
    currency: CurrencyLiteral
    note: Annotated[str | None, Field(max_length=2000)] = None

    @field_validator("amount")
    @classmethod
    def _non_zero(cls, value: float) -> float:
        if value == 0:
            raise ValueError("amount must be non-zero")
        return value
