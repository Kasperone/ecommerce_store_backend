"""ReturnRequest / ReturnItem models — the customer RMA (returns) workflow.

A return request records that a customer wants to send goods back from a
**delivered** order. The status machine is::

    requested → approved | rejected
    approved  → received | refunded
    received  → refunded

``received`` gates the refund+restock decision until the goods physically
arrive (refunding straight from ``approved`` is allowed for the
ship-back-not-required flow — the admin decides). There is no customer cancel
in v1: an admin rejects instead.

**Refund reuse, no duplication**: the refund-from-return route delegates to the
existing ``crud_refund.create_for_order`` choke point with the return's lines
as the per-line picker, so KOR issuance, the running-total / per-line guards,
restock movements (variant-aware), and the order-status flip all come for
free. ``refund_id`` is UNIQUE — exactly one refund per return; a
partial-refund-then-second-refund flow for a single return is out of scope
(documented v1 limitation). **Restock happens at refund time** (the existing
refund semantics), not at receive.

Eligibility lives in ``crud_return_request``: order ``status == delivered``
AND within ``store_config.returns_window_days`` of delivery
(``orders.delivered_at``, falling back to ``updated_at`` for legacy rows).
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.money import quantize_money
from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.order import Order, OrderItem
    from app.models.refund import Refund
    from app.models.user import User


class ReturnStatus(str, enum.Enum):
    """Where a return request is in its lifecycle.

    Stored as the lowercase value in a plain ``String(16)`` column (not a PG
    enum) so adding a status is a code-only change with no migration. Mirrors
    ``RefundReason`` / ``QuoteStatus``.
    """

    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    RECEIVED = "received"
    REFUNDED = "refunded"


# Mirror of ``ReturnStatus`` values, used by the DB CHECK constraint and
# cross-checked by ``app/schemas/return_request.py::ReturnStatusLiteral`` so
# the route layer, DB layer, and OpenAPI schema can never drift apart.
RETURN_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in ReturnStatus)


class ReturnReason(str, enum.Enum):
    """Why the customer wants to return a line (per ``return_items`` row).

    Same plain-``String`` + CHECK convention as ``ReturnStatus``.
    """

    DEFECTIVE = "defective"
    WRONG_ITEM = "wrong_item"
    NOT_AS_DESCRIBED = "not_as_described"
    NO_LONGER_NEEDED = "no_longer_needed"
    OTHER = "other"


# Mirror of ``ReturnReason`` values — see ``RETURN_STATUS_VALUES``.
RETURN_REASON_VALUES: tuple[str, ...] = tuple(r.value for r in ReturnReason)


class ReturnRequest(BaseModel):
    """A customer's request to return (some of) a delivered order's goods.

    ``order_id`` is ON DELETE RESTRICT — a return is part of the order's
    legal/financial trail and cannot disappear with it (orders are never
    hard-deleted anyway). ``user_id`` is the requester, ON DELETE SET NULL so
    the row survives GDPR account deletion (same rationale as the refund actor
    FK). ``refund_id`` links the refund issued *for* this return (SET NULL +
    UNIQUE — one refund per return); the returnable-qty formula excludes that
    linked refund's quantities to avoid double-counting.

    ``comment`` is the customer's free text; ``admin_note`` is internal (the
    customer-facing surface drops it client-side, same convention as the
    refund ``note``). ``resolved_at`` is set when the request reaches a
    terminal state (``rejected`` / ``refunded``).
    """

    __tablename__ = "return_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "', '".join(RETURN_STATUS_VALUES) + "')",
            name="ck_return_requests_status_enum",
        ),
    )

    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="RESTRICT"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default=ReturnStatus.REQUESTED.value, server_default="requested"
    )
    comment: Mapped[str | None] = mapped_column(Text)
    admin_note: Mapped[str | None] = mapped_column(Text)
    refund_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("refunds.id", ondelete="SET NULL"), unique=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)

    order: Mapped[Order] = relationship("Order")
    user: Mapped[User | None] = relationship("User")
    refund: Mapped[Refund | None] = relationship("Refund")
    # Cascade ``delete-orphan`` matches the FK ON DELETE CASCADE on
    # ``return_items.return_request_id``. Ordered by id for deterministic
    # responses (insertion order).
    items: Mapped[list[ReturnItem]] = relationship(
        "ReturnItem",
        back_populates="return_request",
        cascade="all, delete-orphan",
        order_by="ReturnItem.id",
    )

    # ------------------------------------------------------------------
    # Pydantic ``from_attributes`` denormalisation (mirrors Refund.actor_email)
    # ------------------------------------------------------------------

    @property
    def order_number(self) -> str | None:
        """The parent order's public number (eager-loaded in scoped queries)."""
        return self.order.order_number if self.order is not None else None

    @property
    def currency(self) -> str | None:
        """The parent order's currency (return amounts are order-scoped)."""
        return self.order.currency if self.order is not None else None

    @property
    def customer_email(self) -> str | None:
        """Requester email (None when the account was deleted — SET NULL)."""
        return self.user.email if self.user is not None else None

    def __repr__(self) -> str:
        return f"<ReturnRequest order#{self.order_id} {self.status}>"


class ReturnItem(BaseModel):
    """One returned line on a return request — an ``OrderItem`` + quantity + reason.

    ``return_request_id`` is ON DELETE CASCADE (a deleted request takes its
    picker with it); ``order_item_id`` is ON DELETE RESTRICT for the same
    reason refunds restrict: the row points at the original sale. UNIQUE on
    ``(return_request_id, order_item_id)`` — one row per line per request.
    Variant identity (``variant_id`` / ``variant_title``) lives on the order
    item; restock flows through ``create_for_order``'s variant-aware
    ``apply_delta`` loop with zero RMA-side changes.
    """

    __tablename__ = "return_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_return_items_quantity_positive"),
        CheckConstraint(
            "reason IN ('" + "', '".join(RETURN_REASON_VALUES) + "')",
            name="ck_return_items_reason_enum",
        ),
        UniqueConstraint(
            "return_request_id", "order_item_id", name="uq_return_items_request_order_item"
        ),
    )

    return_request_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("return_requests.id", ondelete="CASCADE"), index=True
    )
    order_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("order_items.id", ondelete="RESTRICT"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(32))

    return_request: Mapped[ReturnRequest] = relationship("ReturnRequest", back_populates="items")
    order_item: Mapped[OrderItem] = relationship("OrderItem")

    # ------------------------------------------------------------------
    # Pydantic ``from_attributes`` denormalisation (mirrors RefundItem)
    # ------------------------------------------------------------------

    @property
    def product_name(self) -> str | None:
        return self.order_item.product_name if self.order_item is not None else None

    @property
    def variant_title(self) -> str | None:
        return self.order_item.variant_title if self.order_item is not None else None

    @property
    def unit_price(self) -> Decimal | None:
        return self.order_item.unit_price if self.order_item is not None else None

    @property
    def line_subtotal(self) -> Decimal | None:
        if self.order_item is None:
            return None
        return quantize_money(self.order_item.unit_price * self.quantity)

    def __repr__(self) -> str:
        return (
            f"<ReturnItem request#{self.return_request_id} "
            f"order_item#{self.order_item_id} x{self.quantity} ({self.reason})>"
        )
