"""Refund model — admin-issued refunds against a paid order.

A refund records that money was returned to the customer. v1 ships **full-order
refunds only** (the admin form hard-codes ``amount = order.total``); the schema
already allows partial amounts so the v2 "per-line picker" upgrade is a UI-only
change. Multiple refund rows per order are allowed by the FK shape so partial
refunds and the running-total guard (``sum(refunds) <= order.total``) slot in
cleanly.

The state machine is dedicated: an admin issues refunds via
``POST /api/v1/admin/orders/{id}/refunds`` (admin PATCH explicitly rejects
``status=refunded``, mirroring the cancel-endpoint pattern). The single arithmetic
entry point is ``crud_refund.create_for_order`` — same caller-owns-the-commit
contract as ``crud_inventory.apply_delta`` and ``crud_order.cancel``.

Provider-side refund (``stripe.refunds.create`` / PayU equivalent) is gated on
having keys; v1 records the refund and leaves ``provider_refund_id`` NULL. The
follow-up provider-integration PR populates it.
"""

from __future__ import annotations

import enum
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.corrective_invoice import CorrectiveInvoice
    from app.models.order import Order
    from app.models.refund_item import RefundItem
    from app.models.user import User


class RefundReason(str, enum.Enum):
    """Why a refund was issued.

    Stored as the lowercase value in a plain ``String(32)`` column (not a PG
    enum) so adding a reason is a code-only change with no migration. Mirrors
    ``InventoryMovementReason``.
    """

    DEFECTIVE = "defective"
    WRONG_ITEM = "wrong_item"
    CUSTOMER_REQUEST = "customer_request"
    LOST_IN_TRANSIT = "lost_in_transit"
    DAMAGED_IN_TRANSIT = "damaged_in_transit"
    OTHER = "other"


# Mirror of ``RefundReason.values``, used by the DB CHECK constraint and
# referenced by ``app/schemas/refund.py::RefundReasonLiteral`` so the route layer,
# DB layer, and OpenAPI schema can never drift apart.
REFUND_REASON_VALUES: tuple[str, ...] = tuple(r.value for r in RefundReason)


class Refund(BaseModel):
    """A recorded refund against a paid order.

    ``order_id`` is ON DELETE RESTRICT — a refund is a legal/financial record and
    cannot disappear with its order (orders are never hard-deleted anyway).
    ``actor_user_id`` is ON DELETE SET NULL so the row survives the deletion of
    the issuing admin (same rationale as the audit + inventory actor FKs); the
    response schema then falls back to a placeholder label.

    ``currency`` is snapshotted from the order at issue time so a refund renders
    correctly even after we add multi-currency-per-order support; for v1 it's
    always equal to ``order.currency``.

    ``restock`` records the admin's decision at issue time (the dialog defaults
    it based on order status per D2). When True, ``CRUDRefund.create_for_order``
    writes one ``order_refunded`` inventory movement per line via
    ``crud_inventory.apply_delta`` (same per-line pattern as cancel).
    """

    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_refunds_amount_positive"),
        CheckConstraint(
            "reason IN ('" + "', '".join(REFUND_REASON_VALUES) + "')",
            name="ck_refunds_reason_enum",
        ),
    )

    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="RESTRICT"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    reason: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    restock: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Populated by the future provider-integration PR (stripe re_xxx / PayU equivalent);
    # NULL means "record-only" (the v1 default).
    provider_refund_id: Mapped[str | None] = mapped_column(String(120))
    actor_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    order: Mapped[Order] = relationship("Order", back_populates="refunds")
    actor: Mapped[User | None] = relationship("User")
    # One faktura korygująca per refund (legally required for a refund against a
    # paid PL invoice). Cascade is deliberately NOT delete-orphan: a KOR is a
    # legal record that outlives its refund row.
    corrective_invoice: Mapped[CorrectiveInvoice | None] = relationship(
        "CorrectiveInvoice", back_populates="refund", uselist=False
    )
    # The v2 per-line picker. Cascade ``delete-orphan`` matches the FK ON DELETE
    # CASCADE on ``refund_items.refund_id``: deleting the refund row takes its
    # picker with it. Order matches insertion order for deterministic responses.
    items: Mapped[list[RefundItem]] = relationship(
        "RefundItem",
        back_populates="refund",
        cascade="all, delete-orphan",
        order_by="RefundItem.id",
    )

    @property
    def actor_email(self) -> str | None:
        """Email of the issuing admin (eager-loaded via the actor relationship).

        Surfaced via Pydantic ``from_attributes`` on ``RefundResponse`` so the
        OrderResponse's ``refunds`` list carries the actor email without a
        per-row N+1. Returns ``None`` if the admin was deleted (``actor_id`` ON
        DELETE SET NULL) — the FE then shows a placeholder.
        """
        return self.actor.email if self.actor is not None else None

    def __repr__(self) -> str:
        return f"<Refund order#{self.order_id} {self.amount} {self.currency} ({self.reason})>"
