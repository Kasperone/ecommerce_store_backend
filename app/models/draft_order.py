"""Draft-order models — admin-composed orders-in-progress (Shopify-style).

A ``DraftOrder`` is a header (optional customer or guest email, a pinned
``currency``, optional shipping fields, a manual ``discount``, free-form
``notes``, a lifecycle ``status``, and server-computed ``subtotal`` /
``total``) plus a set of ``DraftOrderItem`` catalog line items. The
parent/child shape mirrors ``Quote``/``QuoteItem`` (``app/models/quote.py``).

**Drafts hold NO stock and create NO orders.** Conversion into a real
``Order`` (stock check, VAT snapshot, shipping resolution, owner
enforcement) is a separate concern (``POST /admin/draft-orders/{id}/convert``,
PR 3) — this module only models the rows.

Unlike a quote line (free-text ``description`` + snapshotted ``unit_price``),
a draft line is a **live catalog reference**: pricing resolves at read time
from the product/variant price in the draft's currency unless the admin pins
a per-line ``unit_price_override``. A deleted product SET-NULLs the line so it
shows visibly stale (``product_name`` → ``None``); conversion 422s on it.

``status`` follows the ``QuoteStatus`` convention: a ``str``-enum stored as
the lowercase value in a plain ``String(20)`` column (not a PG enum), guarded
by a CHECK constraint. ``open`` is the working state; ``completed`` is set by
conversion (PR 3) and locks the draft against further edits.

``subtotal`` / ``total`` are server-computed on every item/discount write
(see ``app/crud/draft_order.py``) — never trusted from the request body.
Shipping cost is deliberately NOT part of draft totals; it resolves at
conversion time (``total = max(subtotal - discount, 0)``).
"""

from __future__ import annotations

import enum
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.money import quantize_money
from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.product_variant import ProductVariant
    from app.models.shipping import ShippingMethod
    from app.models.user import User


class DraftOrderStatus(str, enum.Enum):
    """A draft order's lifecycle status.

    Stored as the lowercase value in a ``String(20)`` column (mirrors
    ``QuoteStatus``). ``OPEN`` is the default working state; ``COMPLETED`` is
    set when the draft is converted into a real order (PR 3) and locks the
    draft against any further mutation (edits raise
    ``DraftOrderLockedError`` → 409).
    """

    OPEN = "open"
    COMPLETED = "completed"


# Mirror of ``DraftOrderStatus.values``, used by the DB CHECK constraint and
# referenced by ``app/schemas/draft_order.py`` so the route layer, DB layer,
# and OpenAPI schema can never drift apart. Mirrors ``QUOTE_STATUS_VALUES``.
DRAFT_ORDER_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in DraftOrderStatus)


class DraftOrder(BaseModel):
    """An admin-composed order-in-progress.

    Both ``user_id`` and ``guest_email`` may be NULL while drafting — an
    admin can start from a blank slate; conversion (PR 3) enforces exactly
    one owner (mirrors ``ck_orders_user_or_guest_email``). ``user_id`` is
    ON DELETE SET NULL so deleting the customer leaves the draft intact.

    ``currency`` is pinned at create time and immutable via the API — every
    override / discount / computed total is denominated in it. Shipping
    fields are all optional (a draft may be saved address-less; conversion
    requires a real address). ``shipping_method_id`` is SET NULL so deleting
    a method never blocks/loses a draft.

    ``converted_order_id`` is the conversion marker + double-convert guard
    (set by PR 3), SET NULL so deleting the order leaves the draft readable
    (mirrors ``quotes.converted_order_id``).
    """

    __tablename__ = "draft_orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "', '".join(DRAFT_ORDER_STATUS_VALUES) + "')",
            name="ck_draft_orders_status_enum",
        ),
        CheckConstraint("discount >= 0", name="ck_draft_orders_discount_non_negative"),
        CheckConstraint("subtotal >= 0", name="ck_draft_orders_subtotal_non_negative"),
        CheckConstraint("total >= 0", name="ck_draft_orders_total_non_negative"),
    )

    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    guest_email: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(20), default="open", server_default="open", index=True
    )
    currency: Mapped[str] = mapped_column(String(3))

    # Optional shipping context (column lengths match ``orders``' posture; the
    # address itself is only required at conversion time, not while drafting).
    shipping_address: Mapped[str | None] = mapped_column(String(500))
    shipping_city: Mapped[str | None] = mapped_column(String(255))
    shipping_postal_code: Mapped[str | None] = mapped_column(String(20))
    shipping_country: Mapped[str | None] = mapped_column(String(100))
    shipping_method_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("shipping_methods.id", ondelete="SET NULL")
    )

    # Manual admin discount off the goods subtotal (NEVER routed through the
    # coupon engine — locked design decision). Denominated in ``currency``.
    discount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0"), server_default="0"
    )
    # Server-maintained preview totals: subtotal = sum of resolved line totals,
    # total = max(subtotal - discount, 0). Shipping is NOT included (it
    # resolves at conversion).
    subtotal: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0"), server_default="0"
    )
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"), server_default="0")
    notes: Mapped[str | None] = mapped_column(Text)

    # Conversion marker + double-convert guard (set by PR 3's convert endpoint).
    converted_order_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )

    # Relationships (``user`` is None on guest/blank drafts)
    user: Mapped[User | None] = relationship("User")
    shipping_method: Mapped[ShippingMethod | None] = relationship("ShippingMethod")
    items: Mapped[list[DraftOrderItem]] = relationship(
        "DraftOrderItem",
        back_populates="draft_order",
        cascade="all, delete-orphan",
        order_by="DraftOrderItem.id",
    )

    @property
    def customer_email(self) -> str | None:
        """Email of the draft's customer (falls back to ``guest_email``; may be None)."""
        return self.user.email if self.user is not None else self.guest_email

    @property
    def item_count(self) -> int:
        """Number of line items (eager-loaded in scoped queries — no N+1)."""
        return len(self.items)

    def __repr__(self) -> str:
        return f"<DraftOrder {self.id} {self.status!r} {self.currency} {self.total}>"


class DraftOrderItem(BaseModel):
    """A single catalog line on a draft order.

    ``draft_order_id`` is ON DELETE CASCADE — deleting a draft takes its lines
    with it (matches the ``delete-orphan`` relationship cascade).
    ``product_id`` / ``variant_id`` are live catalog references, ON DELETE SET
    NULL so a deleted product/variant leaves a **visibly stale** line
    (``product_name`` resolves to ``None``) rather than silently vanishing —
    conversion 422s on such a line.

    ``unit_price_override`` NULL means "price from the catalog in the draft's
    currency, resolved at read/convert time"; a concrete value pins the line
    to an admin-negotiated price. There is deliberately no snapshot column —
    a draft is a working document, not a historical record.
    """

    __tablename__ = "draft_order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_draft_order_items_quantity_positive"),
        CheckConstraint(
            "unit_price_override >= 0",
            name="ck_draft_order_items_unit_price_override_non_negative",
        ),
    )

    draft_order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("draft_orders.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="SET NULL"), index=True
    )
    variant_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("product_variants.id", ondelete="SET NULL"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_override: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    draft_order: Mapped[DraftOrder] = relationship("DraftOrder", back_populates="items")
    product: Mapped[Product | None] = relationship("Product")
    variant: Mapped[ProductVariant | None] = relationship("ProductVariant")

    @property
    def product_name(self) -> str | None:
        """Live product name for display (``None`` when the product was deleted)."""
        return self.product.name if self.product is not None else None

    @property
    def variant_title(self) -> str | None:
        """Live variant title for display (``None`` on variantless/stale lines)."""
        return self.variant.title if self.variant is not None else None

    @property
    def effective_unit_price(self) -> Decimal | None:
        """The price this line resolves to right now, in the draft's currency.

        ``unit_price_override`` when pinned; else the live variant price
        (NULL inherits the product), else the live product price. ``None``
        only when the product was deleted and no override is set — the line
        is visibly stale and contributes 0 to the totals.
        """
        if self.unit_price_override is not None:
            return self.unit_price_override
        currency = self.draft_order.currency
        if self.variant is not None:
            return self.variant.get_price(currency)
        if self.product is not None:
            return self.product.get_price(currency)
        return None

    @property
    def line_total(self) -> Decimal | None:
        """``quantize_money(effective_unit_price * quantity)``; ``None`` when stale."""
        price = self.effective_unit_price
        if price is None:
            return None
        return quantize_money(price * self.quantity)

    def __repr__(self) -> str:
        return (
            f"<DraftOrderItem draft#{self.draft_order_id} "
            f"product#{self.product_id} x{self.quantity}>"
        )
