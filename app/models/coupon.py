"""Coupon model — admin-defined discount codes.

A coupon describes a discount (percent or a fixed amount in a currency), an
optional validity window, an optional minimum order, a global usage cap
(``max_uses``) and an optional per-customer cap (``per_user_limit``). The
discount math / applying it to an order total lives in the redemption layer
(``crud/coupon.py`` + ``crud/order.py``), not here. ``used_count`` is the global
counter (bumped atomically at redeem); ``CouponRedemption`` rows (one per
coupon+user+order) back the per-user cap.

``discount_type`` follows the ``RefundReason`` / ``InventoryMovementReason``
convention: a ``str``-enum stored as the lowercase value in a plain
``String(16)`` column (not a PG enum), guarded by a CHECK constraint.

``is_automatic`` marks coupons the checkout applies on its own (no code typed):
the order-create path and the validate preview scan active automatics and pick
the single best discount (see ``crud/coupon.py:pick_best``). The partial index
``ix_coupons_automatic_active`` keeps that per-checkout scan cheap.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class CouponDiscountType(str, enum.Enum):
    """How a coupon discounts an order.

    Stored as the lowercase value in a ``String(16)`` column (mirrors
    ``RefundReason``). ``PERCENT`` discounts by ``value`` percent (1-100,
    currency-agnostic); ``FIXED`` discounts by ``value`` units of ``currency``;
    ``FREE_SHIPPING`` zeroes the order's shipping line (``value`` is 0 and
    ``currency`` is NULL — the saving is whatever shipping would have cost).
    """

    PERCENT = "percent"
    FIXED = "fixed"
    FREE_SHIPPING = "free_shipping"


# Mirror of ``CouponDiscountType.values``, used by the DB CHECK constraint and
# referenced by ``app/schemas/coupon.py`` so the route layer, DB layer, and
# OpenAPI schema can never drift apart. Mirrors ``REFUND_REASON_VALUES``.
COUPON_DISCOUNT_TYPE_VALUES: tuple[str, ...] = tuple(t.value for t in CouponDiscountType)


class Coupon(BaseModel):
    """An admin-defined discount code.

    ``code`` is stored UPPERCASE (the schema normalises it) and is unique +
    indexed — it's how a coupon is looked up. ``currency`` is required for
    ``fixed`` and NULL for ``percent`` (enforced in the schema, not the DB, so
    the rule stays in one place alongside the value-range checks). ``max_uses``
    NULL means unlimited; ``used_count`` starts at 0 and is bumped by the future
    redemption layer (the admin CRUD never sets it). ``starts_at`` / ``ends_at``
    are an optional validity window (both naive UTC, like every datetime column
    in this app).
    """

    __tablename__ = "coupons"
    __table_args__ = (
        CheckConstraint(
            "discount_type IN ('" + "', '".join(COUPON_DISCOUNT_TYPE_VALUES) + "')",
            name="ck_coupons_discount_type_enum",
        ),
        # free_shipping coupons carry value=0 (the saving is the shipping
        # amount, not a stored number); percent/fixed still require > 0.
        CheckConstraint(
            "value > 0 OR discount_type = 'free_shipping'",
            name="ck_coupons_value_positive",
        ),
        CheckConstraint("used_count >= 0", name="ck_coupons_used_count_non_negative"),
        CheckConstraint(
            "per_user_limit IS NULL OR per_user_limit > 0",
            name="ck_coupons_per_user_limit_positive",
        ),
        # Backs the per-checkout automatic-discount scan (pick_best): only
        # automatic+active rows are ever candidates, so index just those.
        Index(
            "ix_coupons_automatic_active",
            "id",
            postgresql_where=text("is_automatic AND is_active"),
        ),
    )

    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    discount_type: Mapped[str] = mapped_column(String(16))
    value: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    min_order_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # Global cap across all customers (NULL = unlimited).
    max_uses: Mapped[int | None] = mapped_column(Integer)
    # Per-customer cap (NULL = unlimited per user). Independent of max_uses, so a
    # coupon can be e.g. "1000 total uses, max 1 per customer" or any subset. The
    # check counts rows in ``coupon_redemptions`` for (coupon, user).
    per_user_limit: Mapped[int | None] = mapped_column(Integer)
    used_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # Applied by checkout automatically (no code typed). All eligibility rules
    # (window / caps / currency / min-order) still apply; the picker chooses the
    # single best discount among automatics + an optionally typed code.
    is_automatic: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    def __repr__(self) -> str:
        return f"<Coupon {self.code} ({self.discount_type}={self.value})>"


class CouponRedemption(BaseModel):
    """One successful redemption of a coupon by a user on an order.

    Backs the per-user cap (``Coupon.per_user_limit``): the count of rows for a
    (coupon_id, user_id) pair is the number of times that customer has used the
    code. Written in the same unit of work as the order, so a rolled-back order
    never records a redemption. ``order_id`` is unique — one order redeems at
    most one coupon, and re-running settlement never double-records.
    """

    __tablename__ = "coupon_redemptions"
    __table_args__ = (Index("ix_coupon_redemptions_coupon_user", "coupon_id", "user_id"),)

    coupon_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("coupons.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="CASCADE"), unique=True
    )

    def __repr__(self) -> str:
        return (
            f"<CouponRedemption coupon={self.coupon_id} user={self.user_id} order={self.order_id}>"
        )
