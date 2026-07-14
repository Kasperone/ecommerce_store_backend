"""Pydantic schemas for gift cards (admin management + public check).

Wire discipline: money is a JSON number (``float``) on the wire while storage +
math stay ``Decimal`` (see ``app/core/money.py``); pydantic coerces the ORM
``Decimal`` on the way out. The plaintext ``code`` appears ONLY on
``GiftCardIssueResponse`` (the 201) — every other shape carries just ``last4``
(D4: codes are bearer money, hashed at rest, no resend).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import EmailStr, Field, field_validator

from app.schemas.base import BaseSchema, TimestampSchema

# The supported storefront currencies — mirrors ``app.schemas.account`` /
# ``app.schemas.coupon`` (kept in sync deliberately).
CurrencyLiteral = Literal["USD", "PLN", "EUR"]

GiftCardStatusLiteral = Literal["active", "disabled"]


class GiftCardBase(BaseSchema):
    """Shared gift-card fields (response shape; plaintext code never here)."""

    last4: str
    initial_value: float
    balance: float
    currency: str
    status: str
    expires_at: datetime | None = None
    recipient_email: str | None = None
    user_id: int | None = None
    note: str | None = None


class GiftCardIssueRequest(BaseSchema):
    """Request body for ``POST /admin/gift-cards`` (issue a card).

    ``locale`` picks the issue-email language (EN/PL) when ``recipient_email``
    is given; ``user_id`` optionally links the card to a customer account
    (metadata only — redemption stays bearer, D8).
    """

    initial_value: Annotated[float, Field(gt=0)]
    currency: CurrencyLiteral
    expires_at: datetime | None = None
    recipient_email: EmailStr | None = None
    user_id: int | None = None
    note: Annotated[str | None, Field(max_length=2000)] = None
    locale: Literal["en", "pl"] = "en"


class GiftCardUpdate(BaseSchema):
    """Request body for ``PATCH /admin/gift-cards/{id}`` (all fields optional).

    Only fields explicitly sent are applied; an explicit ``null`` clears
    ``expires_at`` / ``note``. Balance is NOT patchable — use the adjust
    endpoint so every movement lands in the ledger.
    """

    status: GiftCardStatusLiteral | None = None
    expires_at: datetime | None = None
    note: Annotated[str | None, Field(max_length=2000)] = None


class GiftCardAdjustRequest(BaseSchema):
    """Request body for ``POST /admin/gift-cards/{id}/adjust`` (signed amount)."""

    amount: float
    note: Annotated[str | None, Field(max_length=2000)] = None

    @field_validator("amount")
    @classmethod
    def _non_zero(cls, value: float) -> float:
        if value == 0:
            raise ValueError("amount must be non-zero")
        return value


class GiftCardEntryResponse(BaseSchema):
    """One ledger row (signed ``amount`` + the ``balance_after`` it produced)."""

    id: int
    amount: float
    kind: str
    balance_after: float
    order_id: int | None = None
    actor_user_id: int | None = None
    note: str | None = None
    created_at: datetime


class GiftCardResponse(GiftCardBase, TimestampSchema):
    """A gift card as the admin API returns it (id + timestamps, no code)."""


class GiftCardIssueResponse(GiftCardResponse):
    """The 201 issue response — the ONLY response carrying the plaintext code.

    ``code`` is dash-formatted (``XXXX-XXXX-XXXX-XXXX``) for display; the check
    endpoint normalizes separators/case on lookup.
    """

    code: str


class GiftCardDetailResponse(GiftCardResponse):
    """Detail shape: the card plus its ledger, newest first."""

    entries: list[GiftCardEntryResponse]


class GiftCardListResponse(BaseSchema):
    """Paginated gift-card list (house envelope)."""

    items: list[GiftCardResponse]
    total: int
    page: int
    page_size: int
    pages: int


class GiftCardCheckRequest(BaseSchema):
    """Request body for the public ``POST /gift-cards/check``."""

    code: Annotated[str, Field(min_length=1, max_length=64)]
    currency: CurrencyLiteral


class GiftCardCheckResponse(BaseSchema):
    """Public check result — balance preview only, never card metadata."""

    last4: str
    balance: float
    currency: str
