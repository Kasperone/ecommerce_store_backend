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
``String(8)`` column (not a PG enum), guarded by a CHECK constraint. Adding a
type later is a code-only change with no migration.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class CouponDiscountType(str, enum.Enum):
    """How a coupon discounts an order.

    Stored as the lowercase value in a ``String(8)`` column (mirrors
    ``RefundReason``). ``PERCENT`` discounts by ``value`` percent (1-100,
    currency-agnostic); ``FIXED`` discounts by ``value`` units of ``currency``.
    """

    PERCENT = "percent"
    FIXED = "fixed"


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
        CheckConstraint("value > 0", name="ck_coupons_value_positive"),
        CheckConstraint("used_count >= 0", name="ck_coupons_used_count_non_negative"),
        CheckConstraint(
            "per_user_limit IS NULL OR per_user_limit > 0",
            name="ck_coupons_per_user_limit_positive",
        ),
    )

    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    discount_type: Mapped[str] = mapped_column(String(8))
    value: Mapped[float] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(3))
    min_order_amount: Mapped[float | None] = mapped_column(Float)
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
