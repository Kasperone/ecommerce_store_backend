"""Pydantic schemas for admin coupon management.

``CouponDiscountTypeLiteral`` is hardcoded (pyright needs literal arguments in
type positions) but cross-checked against the model's
``COUPON_DISCOUNT_TYPE_VALUES`` at import time, so the DB CHECK, the schema, and
the route can't drift apart silently. Mirrors ``RefundReasonLiteral``.

Cross-field validation lives here (one place):
- ``value`` is 1-100 for ``percent``, ``> 0`` for ``fixed``, and exactly 0 for
  ``free_shipping`` (the saving is the shipping amount, not a stored number).
- ``currency`` is required for ``fixed`` and must be NULL for ``percent`` and
  ``free_shipping``.
- ``ends_at`` must be strictly after ``starts_at`` when both are set.

Redemption (applying a coupon to an order) is out of scope, so there is no
"redeem" schema and ``used_count`` is read-only on the response.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.models.coupon import COUPON_DISCOUNT_TYPE_VALUES
from app.schemas.base import BaseSchema, TimestampSchema

# Hardcoded for pyright; the cross-check below catches drift from the model.
CouponDiscountTypeLiteral = Literal["percent", "fixed", "free_shipping"]

# Import-time drift catch: a discount type added to one side without the other
# fails the import (and so every dependent route/test) with a clear message.
assert set(CouponDiscountTypeLiteral.__args__) == set(COUPON_DISCOUNT_TYPE_VALUES), (  # noqa: S101
    "CouponDiscountTypeLiteral drifted from app.models.coupon.COUPON_DISCOUNT_TYPE_VALUES: "
    f"schema={set(CouponDiscountTypeLiteral.__args__)} vs model={set(COUPON_DISCOUNT_TYPE_VALUES)}"
)

CurrencyLiteral = Literal["USD", "PLN", "EUR"]


def validate_value_and_currency(discount_type: str, value: float, currency: str | None) -> None:
    """Enforce the per-type value range + currency rule. Raises ``ValueError``."""
    if discount_type == "percent":
        if not (1 <= value <= 100):
            raise ValueError("percent coupons must have value between 1 and 100")
        if currency is not None:
            raise ValueError("percent coupons must not set a currency")
    elif discount_type == "fixed":
        if value <= 0:
            raise ValueError("fixed coupons must have value greater than 0")
        if currency is None:
            raise ValueError("fixed coupons require a currency")
    elif discount_type == "free_shipping":
        if value != 0:
            raise ValueError("free_shipping coupons must have value 0")
        if currency is not None:
            raise ValueError("free_shipping coupons must not set a currency")


def validate_window(starts_at: datetime | None, ends_at: datetime | None) -> None:
    """``ends_at`` must be strictly after ``starts_at`` when both are set."""
    if starts_at is not None and ends_at is not None and ends_at <= starts_at:
        raise ValueError("ends_at must be after starts_at")


class CouponBase(BaseSchema):
    """Shared coupon fields (creation + response).

    ``code`` is normalised to UPPERCASE so lookups are case-insensitive and the
    unique constraint is meaningful regardless of how an admin types it.
    """

    code: Annotated[str, Field(min_length=1, max_length=64)]
    description: str | None = Field(default=None, max_length=2000)
    discount_type: CouponDiscountTypeLiteral
    # ``ge=0`` (not gt) because free_shipping coupons carry value=0; the
    # per-type minimums live in ``validate_value_and_currency``.
    value: float = Field(default=0, ge=0)
    currency: CurrencyLiteral | None = None
    min_order_amount: float | None = Field(default=None, ge=0)
    max_uses: int | None = Field(default=None, ge=1)
    per_user_limit: int | None = Field(default=None, ge=1)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool = True
    # Applied by checkout automatically (no code typed); see pick_best.
    is_automatic: bool = False

    @model_validator(mode="after")
    def _check_code_value_currency_window(self) -> CouponBase:
        self.code = self.code.strip().upper()
        if not self.code:
            raise ValueError("code must not be blank")
        validate_value_and_currency(self.discount_type, self.value, self.currency)
        validate_window(self.starts_at, self.ends_at)
        return self


class CouponCreate(CouponBase):
    """Request body for ``POST /api/v1/admin/coupons``."""


class CouponUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/admin/coupons/{id}`` (all fields optional).

    Cross-field rules (value/currency by type, ``ends_at`` > ``starts_at``) are
    re-validated in the route against the **merged** state, because a PATCH may
    change only one half of a pair (e.g. flip ``discount_type`` without resending
    ``currency``). ``used_count`` is intentionally absent — it's managed by the
    redemption layer, not the admin form.
    """

    code: Annotated[str | None, Field(min_length=1, max_length=64)] = None
    description: str | None = Field(default=None, max_length=2000)
    discount_type: CouponDiscountTypeLiteral | None = None
    # ``ge=0`` so a PATCH can zero the value when flipping to free_shipping;
    # the merged-state route validation enforces the per-type minimums.
    value: float | None = Field(default=None, ge=0)
    currency: CurrencyLiteral | None = None
    min_order_amount: float | None = Field(default=None, ge=0)
    max_uses: int | None = Field(default=None, ge=1)
    per_user_limit: int | None = Field(default=None, ge=1)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool | None = None
    is_automatic: bool | None = None

    @model_validator(mode="after")
    def _normalise_code(self) -> CouponUpdate:
        if self.code is not None:
            self.code = self.code.strip().upper()
            if not self.code:
                raise ValueError("code must not be blank")
        return self


class CouponResponse(CouponBase, TimestampSchema):
    """A coupon row as the API returns it."""

    used_count: int


class CouponListResponse(BaseSchema):
    """Paginated coupon list."""

    items: list[CouponResponse]
    total: int
    page: int
    page_size: int
    pages: int


class CouponValidateRequest(BaseSchema):
    """Request body for ``POST /api/v1/coupons/validate`` (checkout preview).

    ``code`` is optional: with a code the endpoint validates it (400 on failure);
    without one it still previews any applicable **automatic** discount.
    ``subtotal`` is the storefront's current goods total in ``currency``;
    ``shipping_amount`` is the shipping the cart would otherwise pay — it's what
    a ``free_shipping`` coupon saves (post free-above-threshold, so send the
    already-resolved amount). The code is uppercased so the lookup matches the
    canonical stored value regardless of how the customer typed it; a blank code
    is treated as absent.
    """

    code: Annotated[str | None, Field(max_length=64)] = None
    subtotal: float = Field(..., ge=0)
    shipping_amount: float = Field(default=0, ge=0)
    currency: CurrencyLiteral

    @model_validator(mode="after")
    def _normalise_code(self) -> CouponValidateRequest:
        if self.code is not None:
            self.code = self.code.strip().upper() or None
        return self


class CouponValidateResponse(BaseSchema):
    """Preview of the discount checkout would apply (typed code or automatic).

    Advisory only — the order-create path re-validates and re-computes against the
    server-side subtotal, so a stale preview can never let an invalid code through.
    Routed through the same picker as checkout (``pick_best``), so the preview
    can never diverge from what the order actually gets.

    The applied-fields (``code`` / ``discount_type`` / ``discount_amount``) are
    NULL on the "nothing applies" 200 (no code typed and no automatic matched).
    ``discount_amount`` is the goods discount for percent/fixed and the shipping
    saved for free_shipping; ``free_shipping=True`` means the storefront should
    show shipping struck through to 0 instead of a goods-discount line.
    """

    code: str | None
    discount_type: CouponDiscountTypeLiteral | None
    discount_amount: float | None
    is_automatic: bool
    free_shipping: bool
    currency: CurrencyLiteral
