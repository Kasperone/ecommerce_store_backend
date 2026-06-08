"""CRUDReturnRequest — eligibility, returnable-qty math, and the status machine.

Eligibility (the customer-facing gate, also enforced on create):

- order ``status == delivered`` (a refunded/partially-refunded order is no
  longer "delivered" — its return path is closed; v1 accepted),
- ``store_config.returns_window_days > 0`` (0 = returns disabled),
- ``now <= delivery anchor + window`` where the anchor is
  ``orders.delivered_at`` falling back to ``updated_at`` for legacy delivered
  rows (orders only started recording ``delivered_at`` with this feature),
- and (for the boolean verdict) at least one returnable unit remains.

Returnable qty per order_item::

    quantity
    - Σ return_items.quantity over this order's returns with status != 'rejected'
    - Σ refund_items.quantity over this order's refunds NOT linked from any
      return_request (the ``return_requests.refund_id`` exclusion prevents
      double-counting the refund issued *for* a return)

Concurrency note (v1, documented in the plan §C.7): the returnable-qty check
is read-then-write — two simultaneous requests for the same line can briefly
over-ask; the worst case is the admin rejecting the over-ask. No
``SELECT … FOR UPDATE`` taken.

No commits anywhere — the route owns the transaction (same contract as
``crud_refund`` / ``AuditWriter``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.order import Order, OrderStatus
from app.models.refund import Refund
from app.models.refund_item import RefundItem
from app.models.return_request import ReturnItem, ReturnRequest, ReturnStatus
from app.schemas.return_request import ReturnRequestCreate

# Guarded admin transitions. ``refunded`` is reachable from BOTH ``received``
# (the default flow — goods arrived, then refund) and ``approved`` (the
# ship-back-not-required flow); it is only ever entered via the
# refund-from-return route, never via a bare transition endpoint.
RETURN_TRANSITIONS: dict[ReturnStatus, set[ReturnStatus]] = {
    ReturnStatus.REQUESTED: {ReturnStatus.APPROVED, ReturnStatus.REJECTED},
    ReturnStatus.APPROVED: {ReturnStatus.RECEIVED, ReturnStatus.REFUNDED},
    ReturnStatus.RECEIVED: {ReturnStatus.REFUNDED},
    ReturnStatus.REJECTED: set(),
    ReturnStatus.REFUNDED: set(),
}

# Statuses a refund may be issued from (plan §C.2: received AND approved).
REFUNDABLE_RETURN_STATUSES: set[ReturnStatus] = {
    ReturnStatus.RECEIVED,
    ReturnStatus.APPROVED,
}

# Terminal statuses → ``resolved_at`` is stamped on entry.
_TERMINAL: set[ReturnStatus] = {ReturnStatus.REJECTED, ReturnStatus.REFUNDED}


def _utcnow_naive() -> datetime:
    """Naive UTC now (all datetime columns are naive UTC by convention)."""
    return datetime.now(UTC).replace(tzinfo=None)


class OrderNotReturnableError(Exception):
    """The order is not delivered (only delivered orders can be returned)."""

    def __init__(self, status: OrderStatus | str) -> None:
        self.status = OrderStatus(status).value
        super().__init__(
            f"Cannot request a return for an order with status '{self.status}'. "
            "Only delivered orders are returnable."
        )


class ReturnsDisabledError(Exception):
    """``store_config.returns_window_days == 0`` — returns are disabled."""

    def __init__(self) -> None:
        super().__init__("Returns are disabled for this store.")


class ReturnWindowClosedError(Exception):
    """The order was delivered longer ago than the returns window allows."""

    def __init__(self, window_ends_at: datetime) -> None:
        self.window_ends_at = window_ends_at
        super().__init__(
            f"The return window for this order closed on {window_ends_at.date().isoformat()}."
        )


class ReturnLineNotOnOrderError(Exception):
    """A picked ``order_item_id`` doesn't belong to the parent order."""

    def __init__(self, order_item_id: int, order_id: int) -> None:
        self.order_item_id = order_item_id
        self.order_id = order_id
        super().__init__(f"Order item {order_item_id} does not belong to order {order_id}.")


class ReturnLineOverError(Exception):
    """Requested qty exceeds the line's remaining returnable quantity."""

    def __init__(
        self,
        *,
        order_item_id: int,
        product_name: str,
        requested_qty: int,
        returnable_qty: int,
    ) -> None:
        self.order_item_id = order_item_id
        self.product_name = product_name
        self.requested_qty = requested_qty
        self.returnable_qty = returnable_qty
        super().__init__(
            f"Cannot return {requested_qty} units of '{product_name}': only "
            f"{returnable_qty} remain returnable."
        )


class ReturnTransitionError(Exception):
    """An illegal status transition was requested (admin workflow)."""

    def __init__(self, current: ReturnStatus | str, target: ReturnStatus | str) -> None:
        self.current = ReturnStatus(current).value
        self.target = ReturnStatus(target).value
        super().__init__(
            f"Cannot transition a return request from '{self.current}' to '{self.target}'."
        )


class CRUDReturnRequest:
    """Return-request choke point + scoped reads (no commit; caller owns the txn)."""

    # ------------------------------------------------------------------
    # Eligibility / returnable-qty math
    # ------------------------------------------------------------------

    def delivery_anchor(self, order: Order) -> datetime:
        """The timestamp the returns window counts from.

        ``delivered_at`` when recorded; legacy delivered orders (rows that
        flipped to DELIVERED before the column existed) fall back to
        ``updated_at`` — the status flip was their last update in practice.

        Normalized to naive UTC: a DB round-trip already yields naive values,
        but a not-yet-refreshed instance still carries the aware Python-side
        column default (``BaseModel._utcnow``).
        """
        anchor = order.delivered_at or order.updated_at
        if anchor.tzinfo is not None:
            anchor = anchor.astimezone(UTC).replace(tzinfo=None)
        return anchor

    def window_ends_at(self, order: Order, *, window_days: int) -> datetime:
        return self.delivery_anchor(order) + timedelta(days=window_days)

    def returnable_qty_by_line(self, db: Session, *, order: Order) -> dict[int, int]:
        """``{order_item_id: returnable_qty}`` for every line on the order.

        Per the plan's formula: line quantity, minus quantities on this order's
        non-rejected returns, minus quantities on this order's refunds that are
        NOT the linked refund of any return (linked refunds were already
        counted via their return's items — subtracting both would double-count).
        Floored at 0 (a goodwill over-refund must not produce a negative).
        """
        # Σ return_items.qty per line over non-rejected returns of this order.
        returned_rows = (
            db.query(ReturnItem.order_item_id, func.sum(ReturnItem.quantity))
            .join(ReturnRequest, ReturnItem.return_request_id == ReturnRequest.id)
            .filter(
                ReturnRequest.order_id == order.id,
                ReturnRequest.status != ReturnStatus.REJECTED.value,
            )
            .group_by(ReturnItem.order_item_id)
            .all()
        )
        returned_by_line: dict[int, int] = {row[0]: int(row[1]) for row in returned_rows}

        # Σ refund_items.qty per line over refunds NOT linked from any return.
        linked_refund_ids = (
            db.query(ReturnRequest.refund_id)
            .filter(ReturnRequest.order_id == order.id, ReturnRequest.refund_id.isnot(None))
            .scalar_subquery()
        )
        refunded_rows = (
            db.query(RefundItem.order_item_id, func.sum(RefundItem.quantity))
            .join(Refund, RefundItem.refund_id == Refund.id)
            .filter(Refund.order_id == order.id, Refund.id.notin_(linked_refund_ids))
            .group_by(RefundItem.order_item_id)
            .all()
        )
        refunded_by_line: dict[int, int] = {row[0]: int(row[1]) for row in refunded_rows}

        return {
            item.id: max(
                item.quantity - returned_by_line.get(item.id, 0) - refunded_by_line.get(item.id, 0),
                0,
            )
            for item in order.items
        }

    def check_eligibility(
        self, db: Session, *, order: Order, window_days: int
    ) -> tuple[bool, str | None, datetime | None, dict[int, int]]:
        """Return ``(eligible, reason, window_ends_at, returnable_by_line)``.

        ``reason`` is a machine-readable code (``not_delivered`` /
        ``returns_disabled`` / ``window_closed`` / ``nothing_returnable``),
        None when eligible. ``window_ends_at`` is None until the order is
        delivered / when returns are disabled. The same checks gate the create
        path (as typed exceptions), so the preview can never diverge.
        """
        if OrderStatus(order.status) != OrderStatus.DELIVERED:
            return False, "not_delivered", None, {}
        if window_days <= 0:
            return False, "returns_disabled", None, {}
        ends_at = self.window_ends_at(order, window_days=window_days)
        returnable = self.returnable_qty_by_line(db, order=order)
        if _utcnow_naive() > ends_at:
            return False, "window_closed", ends_at, returnable
        if not any(qty > 0 for qty in returnable.values()):
            return False, "nothing_returnable", ends_at, returnable
        return True, None, ends_at, returnable

    # ------------------------------------------------------------------
    # Creation (customer)
    # ------------------------------------------------------------------

    def create_for_order(
        self,
        db: Session,
        *,
        order: Order,
        user_id: int | None,
        obj_in: ReturnRequestCreate,
        window_days: int,
    ) -> ReturnRequest:
        """Validate eligibility + per-line quantities, write the request + lines.

        Validation order (the route maps all of these to 400):

        1. ``OrderNotReturnableError`` — order status is not ``delivered``.
        2. ``ReturnsDisabledError`` — ``window_days == 0``.
        3. ``ReturnWindowClosedError`` — past the delivery anchor + window.
        4. Per line: ``ReturnLineNotOnOrderError`` / ``ReturnLineOverError``
           (cumulative within the request too — the DB UNIQUE would catch a
           duplicate line at flush, but we surface a clean error first).

        Flushes so ``id`` is populated; does not commit.
        """
        if OrderStatus(order.status) != OrderStatus.DELIVERED:
            raise OrderNotReturnableError(order.status)
        if window_days <= 0:
            raise ReturnsDisabledError()
        ends_at = self.window_ends_at(order, window_days=window_days)
        if _utcnow_naive() > ends_at:
            raise ReturnWindowClosedError(ends_at)

        returnable = self.returnable_qty_by_line(db, order=order)
        line_by_id = {item.id: item for item in order.items}
        seen_in_request: dict[int, int] = {}
        for line_in in obj_in.items:
            line = line_by_id.get(line_in.order_item_id)
            if line is None:
                raise ReturnLineNotOnOrderError(line_in.order_item_id, order.id)
            seen_in_request[line.id] = seen_in_request.get(line.id, 0) + line_in.quantity
            if seen_in_request[line.id] > returnable.get(line.id, 0):
                raise ReturnLineOverError(
                    order_item_id=line.id,
                    product_name=line.product_name,
                    requested_qty=seen_in_request[line.id],
                    returnable_qty=returnable.get(line.id, 0),
                )

        ret = ReturnRequest(
            order_id=order.id,
            user_id=user_id,
            status=ReturnStatus.REQUESTED.value,
            comment=obj_in.comment,
        )
        db.add(ret)
        db.flush()  # populate ret.id before writing the lines
        for line_in in obj_in.items:
            db.add(
                ReturnItem(
                    return_request_id=ret.id,
                    order_item_id=line_in.order_item_id,
                    quantity=line_in.quantity,
                    reason=line_in.reason,
                )
            )
        db.flush()
        db.refresh(ret)
        return ret

    # ------------------------------------------------------------------
    # Status machine (admin)
    # ------------------------------------------------------------------

    def transition_error(
        self, current: ReturnStatus | str, target: ReturnStatus | str
    ) -> str | None:
        """Error message if ``current → target`` is not allowed, else None."""
        current = ReturnStatus(current)
        target = ReturnStatus(target)
        if target not in RETURN_TRANSITIONS.get(current, set()):
            return f"Cannot transition a return request from '{current.value}' to '{target.value}'."
        return None

    def apply_transition(
        self,
        db: Session,
        *,
        ret: ReturnRequest,
        target: ReturnStatus,
        admin_note: str | None = None,
    ) -> ReturnRequest:
        """Apply a guarded transition (raises ``ReturnTransitionError`` on an
        illegal move). Stamps ``resolved_at`` on entry into a terminal status.
        Does not flush — the audit writer reads ``get_history`` before the
        route commits.
        """
        error = self.transition_error(ret.status, target)
        if error is not None:
            raise ReturnTransitionError(ret.status, target)
        ret.status = target.value
        if admin_note is not None:
            ret.admin_note = admin_note
        if target in _TERMINAL:
            ret.resolved_at = _utcnow_naive()
        db.add(ret)
        return ret

    def mark_refunded(self, db: Session, *, ret: ReturnRequest, refund_id: int) -> ReturnRequest:
        """Link the issued refund and flip the return to ``refunded``.

        The caller (the refund-from-return route) has already verified the
        return is in a refundable status and issued the refund row via
        ``crud_refund.create_for_order``. Does not flush (audit reads history).
        """
        ret.status = ReturnStatus.REFUNDED.value
        ret.refund_id = refund_id
        ret.resolved_at = _utcnow_naive()
        db.add(ret)
        return ret

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _eager(self):
        """Eager-load options shared by every read (items + their order_item
        for the denormalised response fields, the parent order for
        ``order_number``/``currency``, the requester for ``customer_email``)."""
        return (
            selectinload(ReturnRequest.items).joinedload(ReturnItem.order_item),
            joinedload(ReturnRequest.order),
            joinedload(ReturnRequest.user),
        )

    def get(self, db: Session, *, return_id: int) -> ReturnRequest | None:
        """Single return request with items + order + user eager-loaded."""
        return (
            db.query(ReturnRequest)
            .options(*self._eager())
            .filter(ReturnRequest.id == return_id)
            .first()
        )

    def list_for_order(self, db: Session, *, order_id: int) -> list[ReturnRequest]:
        """All return requests for an order, oldest first (customer view)."""
        return (
            db.query(ReturnRequest)
            .options(*self._eager())
            .filter(ReturnRequest.order_id == order_id)
            .order_by(ReturnRequest.created_at.asc(), ReturnRequest.id.asc())
            .all()
        )

    def get_multi(
        self,
        db: Session,
        *,
        status: ReturnStatus | str | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> list[ReturnRequest]:
        """Admin list, newest first, optional status filter."""
        query = db.query(ReturnRequest).options(*self._eager())
        if status is not None:
            query = query.filter(ReturnRequest.status == ReturnStatus(status).value)
        return (
            query.order_by(ReturnRequest.created_at.desc(), ReturnRequest.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count(self, db: Session, *, status: ReturnStatus | str | None = None) -> int:
        query = db.query(ReturnRequest)
        if status is not None:
            query = query.filter(ReturnRequest.status == ReturnStatus(status).value)
        return query.count()


return_request = CRUDReturnRequest()


__all__ = [
    "CRUDReturnRequest",
    "OrderNotReturnableError",
    "REFUNDABLE_RETURN_STATUSES",
    "RETURN_TRANSITIONS",
    "ReturnLineNotOnOrderError",
    "ReturnLineOverError",
    "ReturnTransitionError",
    "ReturnWindowClosedError",
    "ReturnsDisabledError",
    "return_request",
]
