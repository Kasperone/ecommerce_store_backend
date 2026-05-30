"""RefundItem model — the per-line picker linking a refund to refunded order_items.

A refund refunds at most one row per order item (``uq_refund_items_refund_order_item``)
but multiple refunds CAN each refund the same order_item across rows, subject to
the per-line sum guard in ``CRUDRefund.create_for_order``:
``sum(qty across all refunds for this order_item) <= order_item.quantity``.

The shape is intentionally close to the existing ``OrderItem`` snapshot:
the picker captures *which line* and *how many units* came back. We don't
snapshot ``unit_price`` here — the line's price is already snapshotted in
``OrderItem.unit_price`` and refund line-subtotals are computed from the
joined ``order_item`` row in ``RefundItemResponse``.

``refund_id`` is ON DELETE CASCADE because a refund row deletion (rare —
admins don't normally hard-delete refunds; legal records) should take its
own line picker with it; ``order_item_id`` is ON DELETE RESTRICT for the
same reason refunds restrict on the parent order: a refund picker row is
part of a legal/financial trail that points at the original sale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.order import OrderItem
    from app.models.refund import Refund


class RefundItem(BaseModel):
    """One refunded line on a refund — points at an ``OrderItem`` with a quantity."""

    __tablename__ = "refund_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_refund_items_quantity_positive"),
        UniqueConstraint("refund_id", "order_item_id", name="uq_refund_items_refund_order_item"),
    )

    refund_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("refunds.id", ondelete="CASCADE"), index=True
    )
    order_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("order_items.id", ondelete="RESTRICT"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer)

    refund: Mapped[Refund] = relationship("Refund", back_populates="items")
    order_item: Mapped[OrderItem] = relationship("OrderItem")

    # ------------------------------------------------------------------
    # Pydantic ``from_attributes`` denormalisation
    # ------------------------------------------------------------------
    # These properties surface the joined ``order_item`` row's denormalised
    # fields straight to ``RefundItemResponse`` so every handler that returns
    # a refund (refund POST, refunds GET, order GET, admin PATCH echoing the
    # order) gets the breakdown without a per-handler rebuild. Mirrors the
    # ``Refund.actor_email`` property pattern. Returns ``None`` when the
    # ``order_item`` was hard-deleted (ON DELETE RESTRICT makes this impossible
    # for refunded items, but the property stays defensive).

    @property
    def product_name(self) -> str | None:
        return self.order_item.product_name if self.order_item is not None else None

    @property
    def unit_price(self) -> float | None:
        return self.order_item.unit_price if self.order_item is not None else None

    @property
    def line_subtotal(self) -> float | None:
        if self.order_item is None:
            return None
        return round(self.order_item.unit_price * self.quantity, 2)

    def __repr__(self) -> str:
        return (
            f"<RefundItem refund#{self.refund_id} order_item#{self.order_item_id} "
            f"x{self.quantity}>"
        )
