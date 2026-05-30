"""CRUDRefund — the single choke-point for issuing refunds.

``create_for_order`` is the only entry point that ever inserts a refund row,
writes the ``order_refunded`` inventory movements, and flips the order's status
to ``refunded``. Mirrors the ``crud_order.cancel`` shape 1:1 with three v1
deltas: the amount guard (full-only in v1, ``PartialRefundUnsupported``
otherwise), a different ``InventoryMovementReason`` (``ORDER_REFUNDED``), and an
optional ``restock`` flag (per D2; cancel always restocks). Caller owns the
commit — same contract as ``crud_inventory.apply_delta`` /
``AuditWriter`` / ``crud_invoice.issue_for_order``.

After the refund row exists, a faktura korygująca (KOR) is **always** issued in
the same transaction when the original order has a faktura (D6): rollback
reclaims the gap-free KOR number alongside the refund.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, joinedload, selectinload

from app.crud.corrective_invoice import corrective_invoice as crud_corrective
from app.crud.inventory import inventory as crud_inventory
from app.crud.invoice import invoice as crud_invoice
from app.models.inventory_movement import InventoryMovementReason
from app.models.order import Order, OrderStatus
from app.models.product import Product
from app.models.refund import Refund

REFUNDABLE_STATUSES: set[OrderStatus] = {
    OrderStatus.PAID,
    OrderStatus.PROCESSING,
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
}


class OrderNotRefundableError(Exception):
    """The order's status doesn't allow a refund (D3)."""

    def __init__(self, status: OrderStatus | str) -> None:
        self.status = OrderStatus(status).value
        super().__init__(
            f"Cannot refund an order with status '{self.status}'. "
            "Refundable statuses: paid, processing, shipped, delivered."
        )


class PartialRefundUnsupportedError(Exception):
    """v1 ships full-order refunds only; the schema supports partials for v2."""

    def __init__(self, amount: float, order_total: float) -> None:
        self.amount = amount
        self.order_total = order_total
        super().__init__(
            f"Partial refunds are not yet supported (received {amount}, "
            f"order total {order_total}). v1 requires amount == order.total."
        )


class CRUDRefund:
    """Refund choke-point + scoped reads (no commit; caller owns the txn)."""

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_for_order(self, db: Session, *, order_id: int) -> list[Refund]:
        """All refunds for an order, oldest first, with the actor + KOR eager-loaded.

        Ordered ascending to match how ``Order.refunds`` loads (deterministic for
        diffs + the partial-refund running-total guard the v2 PR will add).
        """
        return (
            db.query(Refund)
            .options(joinedload(Refund.actor), joinedload(Refund.corrective_invoice))
            .filter(Refund.order_id == order_id)
            .order_by(Refund.created_at.asc(), Refund.id.asc())
            .all()
        )

    # ------------------------------------------------------------------
    # Issuance
    # ------------------------------------------------------------------

    def create_for_order(
        self,
        db: Session,
        *,
        order: Order,
        amount: float,
        reason: str,
        note: str | None,
        restock: bool,
        actor_user_id: int | None,
    ) -> Refund:
        """Validate, write the refund row, optionally restock, flip status to refunded.

        Raises ``PartialRefundUnsupportedError`` when ``amount != order.total``
        (v1 full-only guard) and ``OrderNotRefundableError`` when the order's
        status is not in ``REFUNDABLE_STATUSES``. The route layer maps both to 400.

        When ``restock`` is True, writes one ``ORDER_REFUNDED`` movement per line
        via ``crud_inventory.apply_delta`` — the same per-line shape as
        ``crud_order.cancel``. A deleted product is skipped (the inventory choke
        point would have nothing to mutate); the refund row + status flip stand
        regardless.

        Always issues a faktura korygująca **iff** the order has a faktura
        (orders that flipped to paid through ``crud_invoice.issue_for_order``);
        a refund against an order that somehow has no invoice (legacy or a
        future "refund without invoice" path) gets no KOR, intentionally. The KOR
        allocation is atomic + gap-free + scoped to the caller's transaction —
        rollback reclaims the number.
        """
        order_status = OrderStatus(order.status)
        if order_status not in REFUNDABLE_STATUSES:
            raise OrderNotRefundableError(order_status)
        if round(amount, 2) != round(order.total, 2):
            raise PartialRefundUnsupportedError(amount, order.total)

        refund = Refund(
            order_id=order.id,
            amount=round(amount, 2),
            currency=order.currency,
            reason=reason,
            note=note,
            restock=restock,
            actor_user_id=actor_user_id,
        )
        db.add(refund)
        # Flush so refund.id is populated before we write inventory movements +
        # the KOR (both reference it).
        db.flush()

        if restock:
            for item in order.items:
                product = db.get(Product, item.product_id)
                if product is None:
                    continue
                crud_inventory.apply_delta(
                    db,
                    product=product,
                    delta=item.quantity,
                    reason=InventoryMovementReason.ORDER_REFUNDED,
                    actor_user_id=actor_user_id,
                    order_id=order.id,
                )

        original_invoice = crud_invoice.get_by_order_id(db, order.id)
        if original_invoice is not None:
            crud_corrective.issue_for_refund(db, refund=refund, original_invoice=original_invoice)

        order.status = OrderStatus.REFUNDED
        db.add(order)
        db.flush()
        # Refresh so the caller sees the KOR + status without an extra round-trip.
        db.refresh(refund)
        return refund

    def get(self, db: Session, *, refund_id: int) -> Refund | None:
        """Single refund with actor + KOR eager-loaded (admin detail / tests)."""
        return (
            db.query(Refund)
            .options(joinedload(Refund.actor), joinedload(Refund.corrective_invoice))
            .filter(Refund.id == refund_id)
            .first()
        )


refund = CRUDRefund()


__all__ = [
    "CRUDRefund",
    "OrderNotRefundableError",
    "PartialRefundUnsupportedError",
    "REFUNDABLE_STATUSES",
    "refund",
]


# Re-export the order-relationship eager-load so callers in
# ``app/api/v1/orders.py`` (or ``crud_order.get_scoped``) can add it
# without re-importing the relationship attribute name.
def order_refunds_load_options():
    """Return ``selectinload`` options for eagerly loading ``Order.refunds`` + their
    related actor + KOR rows. Used by ``crud_order.get_scoped`` and the admin list."""
    return (
        selectinload(Order.refunds).joinedload(Refund.actor),
        selectinload(Order.refunds).joinedload(Refund.corrective_invoice),
    )
