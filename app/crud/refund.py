"""CRUDRefund — the single choke-point for issuing refunds.

``create_for_order`` is the only entry point that ever inserts a refund row,
writes the ``order_refunded`` inventory movements, and flips the order's status
to ``REFUNDED`` (cumulative refunds == order.total) or ``PARTIALLY_REFUNDED``
(cumulative refunds > 0 but < order.total). Mirrors the ``crud_order.cancel``
shape with three deltas vs the v1 refund flow:

1. **Running-total guard** instead of full-only:
   ``sum(prior refunds) + amount > order.total → RefundOverError``.
2. **Per-line picker** via the new ``refund_items`` rows; if provided, each
   ``order_item_id`` must belong to the order and the cumulative qty across
   all refunds for the same line is capped at ``order_item.quantity``.
3. **Status flip is conditional**: ``REFUNDED`` once the cumulative refund
   total equals ``order.total`` (rounding-tolerant), otherwise
   ``PARTIALLY_REFUNDED``.

Caller owns the commit — same contract as ``crud_inventory.apply_delta`` /
``AuditWriter`` / ``crud_invoice.issue_for_order``.

After the refund row exists, a faktura korygująca (KOR) is **always** issued in
the same transaction when the original order has a faktura: rollback reclaims
the gap-free KOR number alongside the refund.

D6 caveat (v2): the KOR PDF renderer still shows ``order.total → after this
refund`` in its Before/After/Difference table, NOT ``previous-running-total →
after this refund``. Legally a small nuance for partial refunds; flagged as a
v3 TODO in ``app/services/invoice.py``.
"""

from __future__ import annotations

import math

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from app.crud.corrective_invoice import corrective_invoice as crud_corrective
from app.crud.inventory import inventory as crud_inventory
from app.crud.invoice import invoice as crud_invoice
from app.models.inventory_movement import InventoryMovementReason
from app.models.order import Order, OrderStatus
from app.models.product import Product
from app.models.refund import Refund
from app.models.refund_item import RefundItem

REFUNDABLE_STATUSES: set[OrderStatus] = {
    OrderStatus.PAID,
    OrderStatus.PROCESSING,
    OrderStatus.SHIPPED,
    OrderStatus.DELIVERED,
    # PARTIALLY_REFUNDED is refundable too — the customer can take a second
    # partial that may bring the cumulative total to order.total (→ REFUNDED).
    OrderStatus.PARTIALLY_REFUNDED,
}

# Floating-point tolerance for cumulative refund comparisons. Refund amounts are
# rounded to 2 dp before persistence; this lets ``amount == order.total`` survive
# a 1e-9 drift, and a $0.005 over-refund still 400s.
_AMOUNT_TOL = 0.01


class OrderNotRefundableError(Exception):
    """The order's status doesn't allow a refund (D3)."""

    def __init__(self, status: OrderStatus | str) -> None:
        self.status = OrderStatus(status).value
        super().__init__(
            f"Cannot refund an order with status '{self.status}'. "
            "Refundable statuses: paid, processing, shipped, delivered, partially_refunded."
        )


class RefundOverError(Exception):
    """Cumulative refund amount would exceed ``order.total``."""

    def __init__(self, amount: float, prior_total: float, order_total: float) -> None:
        self.amount = amount
        self.prior_total = prior_total
        self.order_total = order_total
        remaining = round(order_total - prior_total, 2)
        super().__init__(
            f"Refund amount {amount} would exceed the order's remaining "
            f"refundable balance ({remaining}). Already refunded: {prior_total}, "
            f"order total: {order_total}."
        )


class RefundLineOverError(Exception):
    """Per-line cumulative refund qty would exceed the original order_item qty."""

    def __init__(
        self,
        *,
        order_item_id: int,
        product_name: str,
        prior_qty: int,
        requested_qty: int,
        line_qty: int,
    ) -> None:
        self.order_item_id = order_item_id
        self.product_name = product_name
        self.prior_qty = prior_qty
        self.requested_qty = requested_qty
        self.line_qty = line_qty
        remaining = line_qty - prior_qty
        super().__init__(
            f"Cannot refund {requested_qty} units of '{product_name}': only "
            f"{remaining} of {line_qty} remain refundable "
            f"(already refunded: {prior_qty})."
        )


class RefundLineNotOnOrderError(Exception):
    """A picked ``order_item_id`` doesn't belong to the parent order."""

    def __init__(self, order_item_id: int, order_id: int) -> None:
        self.order_item_id = order_item_id
        self.order_id = order_id
        super().__init__(f"Order item {order_item_id} does not belong to order {order_id}.")


class RestockRequiresItemsError(Exception):
    """``restock=True`` without a line picker (D5) — nothing to put back."""

    def __init__(self) -> None:
        super().__init__(
            "Cannot restock without a line picker. Either pick items to refund "
            "or set restock=False (goodwill credit)."
        )


class CRUDRefund:
    """Refund choke-point + scoped reads (no commit; caller owns the txn)."""

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_for_order(self, db: Session, *, order_id: int) -> list[Refund]:
        """All refunds for an order, oldest first, with actor + KOR + items eager-loaded.

        Ordered ascending to match how ``Order.refunds`` loads (deterministic for
        diffs + the running-total guard).
        """
        return (
            db.query(Refund)
            .options(
                joinedload(Refund.actor),
                joinedload(Refund.corrective_invoice),
                selectinload(Refund.items).joinedload(RefundItem.order_item),
            )
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
        items: list[tuple[int, int]] | None = None,
    ) -> Refund:
        """Validate, write the refund row + picked lines, optionally restock, flip status.

        ``items`` is a list of ``(order_item_id, quantity)`` tuples representing
        the per-line picker (v2). ``None`` or ``[]`` records a goodwill refund
        with no line attribution.

        Validation order:

        1. ``OrderNotRefundableError`` — order status not in ``REFUNDABLE_STATUSES``.
        2. ``RestockRequiresItemsError`` — ``restock=True`` AND no items picked (D5).
        3. ``RefundOverError`` — cumulative refund amount > ``order.total`` (running total).
        4. Per-line (only when items provided):
           - ``RefundLineNotOnOrderError`` — ``order_item_id`` not on this order.
           - ``RefundLineOverError`` — cumulative refunded qty > ``order_item.quantity``.

        The route layer maps all of the above to 400.

        After validation: write the ``Refund`` row, flush so ``refund.id`` is
        populated, write the ``RefundItem`` rows, and (when ``restock=True``)
        write one ``ORDER_REFUNDED`` movement per picked line via
        ``crud_inventory.apply_delta`` — quantity = picked qty (NOT the original
        line qty, since v2 may restock fewer units than were sold). A deleted
        product is skipped (the inventory choke point would have nothing to
        mutate); the refund row + status flip stand regardless.

        Always issues a faktura korygująca **iff** the order has a faktura.
        Each partial refund gets its own KOR with its own gap-free number.

        Status flip: cumulative refunds == ``order.total`` (rounding-tolerant)
        → ``REFUNDED``; cumulative > 0 but < total → ``PARTIALLY_REFUNDED``.
        """
        order_status = OrderStatus(order.status)
        if order_status not in REFUNDABLE_STATUSES:
            raise OrderNotRefundableError(order_status)

        items_list = items or []

        if restock and not items_list:
            raise RestockRequiresItemsError()

        # Running-total guard (whole-refund amount). Compute from a fresh query so
        # we don't depend on ``order.refunds`` being eagerly loaded or back-populated
        # by a prior refund in the same session (the v1 path set ``Refund.order_id``
        # by FK int, which doesn't trigger ``back_populates``).
        prior_total_raw: float | None = (
            db.query(func.coalesce(func.sum(Refund.amount), 0.0))
            .filter(Refund.order_id == order.id)
            .scalar()
        )
        prior_total = round(prior_total_raw or 0.0, 2)
        amount_r = round(amount, 2)
        if prior_total + amount_r > round(order.total, 2) + _AMOUNT_TOL:
            raise RefundOverError(amount_r, prior_total, round(order.total, 2))

        # Per-line validation (only when items provided).
        if items_list:
            # Build {order_item_id: OrderItem} for the picked-line existence check.
            line_by_id = {item.id: item for item in order.items}
            # Cumulative qty already refunded per order_item across all prior refunds,
            # again via a fresh query for the same reason as the running-total guard.
            prior_qty_rows = (
                db.query(RefundItem.order_item_id, func.sum(RefundItem.quantity))
                .join(Refund, RefundItem.refund_id == Refund.id)
                .filter(Refund.order_id == order.id)
                .group_by(RefundItem.order_item_id)
                .all()
            )
            prior_qty_by_line: dict[int, int] = {row[0]: int(row[1]) for row in prior_qty_rows}
            # Each pick must belong to this order, and cumulative qty must stay ≤ line.
            # Also tally this-request qty per line so the same line picked twice
            # (the schema's UNIQUE blocks it at DB layer, but we surface a clean error).
            seen_in_request: dict[int, int] = {}
            for order_item_id, qty in items_list:
                line = line_by_id.get(order_item_id)
                if line is None:
                    raise RefundLineNotOnOrderError(order_item_id, order.id)
                seen_in_request[order_item_id] = seen_in_request.get(order_item_id, 0) + qty
                cumulative = (
                    prior_qty_by_line.get(order_item_id, 0) + seen_in_request[order_item_id]
                )
                if cumulative > line.quantity:
                    raise RefundLineOverError(
                        order_item_id=order_item_id,
                        product_name=line.product_name,
                        prior_qty=prior_qty_by_line.get(order_item_id, 0),
                        requested_qty=seen_in_request[order_item_id],
                        line_qty=line.quantity,
                    )

        # All checks passed — write the refund + picker rows.
        refund = Refund(
            order_id=order.id,
            amount=amount_r,
            currency=order.currency,
            reason=reason,
            note=note,
            restock=restock,
            actor_user_id=actor_user_id,
        )
        db.add(refund)
        # Flush so refund.id is populated before writing the picker + KOR.
        db.flush()

        for order_item_id, qty in items_list:
            db.add(
                RefundItem(
                    refund_id=refund.id,
                    order_item_id=order_item_id,
                    quantity=qty,
                )
            )

        if restock and items_list:
            # Restock only picked quantities (NOT the original line qty — v2
            # may refund fewer units than were sold).
            line_by_id = {item.id: item for item in order.items}
            for order_item_id, qty in items_list:
                line = line_by_id[order_item_id]
                product = db.get(Product, line.product_id)
                if product is None:
                    continue
                crud_inventory.apply_delta(
                    db,
                    product=product,
                    delta=qty,
                    reason=InventoryMovementReason.ORDER_REFUNDED,
                    actor_user_id=actor_user_id,
                    order_id=order.id,
                )

        original_invoice = crud_invoice.get_by_order_id(db, order.id)
        if original_invoice is not None:
            crud_corrective.issue_for_refund(db, refund=refund, original_invoice=original_invoice)

        # Status flip based on the new cumulative total.
        new_cumulative = prior_total + amount_r
        if math.isclose(new_cumulative, round(order.total, 2), abs_tol=_AMOUNT_TOL):
            order.status = OrderStatus.REFUNDED
        else:
            order.status = OrderStatus.PARTIALLY_REFUNDED
        db.add(order)
        db.flush()
        # Refresh so the caller sees the KOR + status + items without an extra round-trip.
        db.refresh(refund)
        return refund

    def get(self, db: Session, *, refund_id: int) -> Refund | None:
        """Single refund with actor + KOR + items eager-loaded (admin detail / tests)."""
        return (
            db.query(Refund)
            .options(
                joinedload(Refund.actor),
                joinedload(Refund.corrective_invoice),
                selectinload(Refund.items).joinedload(RefundItem.order_item),
            )
            .filter(Refund.id == refund_id)
            .first()
        )


refund = CRUDRefund()


__all__ = [
    "CRUDRefund",
    "OrderNotRefundableError",
    "RefundOverError",
    "RefundLineOverError",
    "RefundLineNotOnOrderError",
    "RestockRequiresItemsError",
    "REFUNDABLE_STATUSES",
    "refund",
]


# Re-export the order-relationship eager-load so callers in
# ``app/api/v1/orders.py`` (or ``crud_order.get_scoped``) can add it
# without re-importing the relationship attribute name.
def order_refunds_load_options():
    """Return ``selectinload`` options for eagerly loading ``Order.refunds`` + their
    related actor + KOR + items rows. Used by ``crud_order.get_scoped`` and the admin
    list."""
    return (
        selectinload(Order.refunds).joinedload(Refund.actor),
        selectinload(Order.refunds).joinedload(Refund.corrective_invoice),
        selectinload(Order.refunds).selectinload(Refund.items).joinedload(RefundItem.order_item),
    )
