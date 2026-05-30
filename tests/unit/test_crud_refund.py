"""Unit tests for ``app/crud/refund.py``.

Covers the refundable-status matrix (D3), the v2 running-total amount guard,
the per-line picker validation (D2), the ``restock`` branch (inventory
movements + product stock), the status flip threshold (REFUNDED at total,
PARTIALLY_REFUNDED below), and the always-issue-KOR side effect when the
order has a faktura. Mirrors the shape of ``tests/unit/test_crud_inventory.py``
/ ``tests/unit/test_crud_invoice.py``.
"""

from __future__ import annotations

import pytest

from app.crud import refund as crud_refund
from app.crud.invoice import invoice as crud_invoice
from app.crud.refund import (
    OrderNotRefundableError,
    RefundLineNotOnOrderError,
    RefundLineOverError,
    RefundOverError,
    RestockRequiresItemsError,
)
from app.models.inventory_movement import InventoryMovement
from app.models.order import Order, OrderItem, OrderStatus
from app.models.refund import RefundReason
from app.models.warehouse import Warehouse


def _make_order(
    db_session, *, user, product, status: OrderStatus = OrderStatus.PAID, quantity: int = 2
) -> Order:
    """Build a pending-shape order on the test session.

    Mirrors what ``crud_order.create_with_items`` would persist but without
    going through the route layer (which validates shipping + stock + a method).
    """
    order = Order(
        user_id=user.id,
        order_number=f"ORD-TEST-{product.id:04d}",
        status=status,
        subtotal=product.price_usd * quantity,
        tax=0.0,
        shipping=0.0,
        total=product.price_usd * quantity,
        currency="USD",
        shipping_address="1 Test St",
        shipping_city="Testville",
        shipping_postal_code="00-001",
        shipping_country="PL",
        items=[
            OrderItem(
                product_id=product.id,
                product_name=product.name,
                product_sku=product.sku,
                quantity=quantity,
                unit_price=product.price_usd,
                total_price=product.price_usd * quantity,
            )
        ],
    )
    db_session.add(order)
    db_session.flush()
    return order


# ----------------------------------------------------------------------
# Refundable-status matrix (D3)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.PAID,
        OrderStatus.PROCESSING,
        OrderStatus.SHIPPED,
        OrderStatus.DELIVERED,
        OrderStatus.PARTIALLY_REFUNDED,
    ],
)
def test_refund_accepts_refundable_statuses(db_session, user_factory, product_factory, status):
    user = user_factory()
    admin = user_factory(email=f"admin-{status.value}@example.com")
    product = product_factory(stock=10, price_usd=12.34)
    order = _make_order(db_session, user=user, product=product, status=status, quantity=2)

    refund = crud_refund.create_for_order(
        db_session,
        order=order,
        amount=order.total,
        reason=RefundReason.CUSTOMER_REQUEST.value,
        note=None,
        restock=False,
        actor_user_id=admin.id,
    )

    assert refund.amount == pytest.approx(order.total)
    assert refund.currency == "USD"
    assert OrderStatus(order.status) == OrderStatus.REFUNDED


@pytest.mark.parametrize(
    "status",
    [OrderStatus.PENDING, OrderStatus.CANCELLED, OrderStatus.REFUNDED],
)
def test_refund_rejects_non_refundable_statuses(db_session, user_factory, product_factory, status):
    user = user_factory()
    admin = user_factory(email=f"admin-bad-{status.value}@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, status=status, quantity=1)

    with pytest.raises(OrderNotRefundableError) as excinfo:
        crud_refund.create_for_order(
            db_session,
            order=order,
            amount=order.total,
            reason=RefundReason.OTHER.value,
            note=None,
            restock=False,
            actor_user_id=admin.id,
        )
    assert excinfo.value.status == status.value


# ----------------------------------------------------------------------
# Amount guard (D1: running-total in v2)
# ----------------------------------------------------------------------


def test_partial_refund_flips_to_partially_refunded(db_session, user_factory, product_factory):
    """Sub-total amount → status PARTIALLY_REFUNDED (was rejected in v1)."""
    user = user_factory()
    admin = user_factory(email="admin-partial@example.com")
    product = product_factory(stock=10, price_usd=20.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID)

    refund = crud_refund.create_for_order(
        db_session,
        order=order,
        amount=order.total - 5,
        reason=RefundReason.CUSTOMER_REQUEST.value,
        note=None,
        restock=False,
        actor_user_id=admin.id,
    )
    assert OrderStatus(order.status) == OrderStatus.PARTIALLY_REFUNDED
    assert refund.amount == pytest.approx(order.total - 5)


def test_over_refund_rejected(db_session, user_factory, product_factory):
    """Amount > order.total → RefundOverError, even on the first refund."""
    user = user_factory()
    admin = user_factory(email="admin-over@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID)

    with pytest.raises(RefundOverError) as excinfo:
        crud_refund.create_for_order(
            db_session,
            order=order,
            amount=order.total + 1,
            reason=RefundReason.OTHER.value,
            note=None,
            restock=False,
            actor_user_id=admin.id,
        )
    assert excinfo.value.prior_total == 0
    assert excinfo.value.order_total == pytest.approx(order.total)


def test_cumulative_over_refund_rejected(db_session, user_factory, product_factory):
    """First refund $30, second refund $25 on a $40 order → second 400s (running guard)."""
    user = user_factory()
    admin = user_factory(email="admin-cumover@example.com")
    product = product_factory(stock=10, price_usd=20.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID)
    # order.total = 40.0 (2 * 20)

    crud_refund.create_for_order(
        db_session,
        order=order,
        amount=30.0,
        reason=RefundReason.CUSTOMER_REQUEST.value,
        note=None,
        restock=False,
        actor_user_id=admin.id,
    )
    assert OrderStatus(order.status) == OrderStatus.PARTIALLY_REFUNDED

    with pytest.raises(RefundOverError):
        crud_refund.create_for_order(
            db_session,
            order=order,
            amount=25.0,
            reason=RefundReason.OTHER.value,
            note=None,
            restock=False,
            actor_user_id=admin.id,
        )


def test_full_refund_accepted_even_with_rounding(db_session, user_factory, product_factory):
    """A 0.005 float drift must not break the guard (we round to 2 dp on both sides)."""
    user = user_factory()
    admin = user_factory(email="admin-rounding@example.com")
    product = product_factory(stock=10, price_usd=19.995)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID)
    drifted = order.total + 0.001  # within rounding tolerance

    refund = crud_refund.create_for_order(
        db_session,
        order=order,
        amount=drifted,
        reason=RefundReason.OTHER.value,
        note=None,
        restock=False,
        actor_user_id=admin.id,
    )
    assert refund.amount == pytest.approx(order.total, abs=0.01)
    # Sum lands at total → REFUNDED, not PARTIALLY_REFUNDED.
    assert OrderStatus(order.status) == OrderStatus.REFUNDED


def test_two_partials_reach_refunded(db_session, user_factory, product_factory):
    """Two partial refunds that sum to order.total flip status to REFUNDED on the second."""
    user = user_factory()
    admin = user_factory(email="admin-twoparts@example.com")
    product = product_factory(stock=10, price_usd=25.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID)
    # order.total = 50.0

    crud_refund.create_for_order(
        db_session,
        order=order,
        amount=20.0,
        reason=RefundReason.CUSTOMER_REQUEST.value,
        note=None,
        restock=False,
        actor_user_id=admin.id,
    )
    assert OrderStatus(order.status) == OrderStatus.PARTIALLY_REFUNDED

    crud_refund.create_for_order(
        db_session,
        order=order,
        amount=30.0,
        reason=RefundReason.OTHER.value,
        note=None,
        restock=False,
        actor_user_id=admin.id,
    )
    assert OrderStatus(order.status) == OrderStatus.REFUNDED


# ----------------------------------------------------------------------
# Per-line picker validation (D2)
# ----------------------------------------------------------------------


def test_pick_unknown_order_item_rejected(db_session, user_factory, product_factory):
    user = user_factory()
    admin = user_factory(email="admin-badpick@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID)

    with pytest.raises(RefundLineNotOnOrderError):
        crud_refund.create_for_order(
            db_session,
            order=order,
            amount=10.0,
            reason=RefundReason.CUSTOMER_REQUEST.value,
            note=None,
            restock=False,
            actor_user_id=admin.id,
            items=[(9999, 1)],  # not on this order
        )


def test_pick_qty_over_line_qty_rejected(db_session, user_factory, product_factory):
    user = user_factory()
    admin = user_factory(email="admin-overpick@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID, quantity=2)
    line = order.items[0]

    with pytest.raises(RefundLineOverError) as excinfo:
        crud_refund.create_for_order(
            db_session,
            order=order,
            amount=order.total,
            reason=RefundReason.DEFECTIVE.value,
            note=None,
            restock=False,
            actor_user_id=admin.id,
            items=[(line.id, 5)],  # only 2 on the line
        )
    assert excinfo.value.line_qty == 2
    assert excinfo.value.requested_qty == 5


def test_pick_cumulative_across_refunds_rejected(db_session, user_factory, product_factory):
    """First refund picks 2 of 3 → second refund picking 2 more is over the line cap.

    Uses small amounts so the running-total guard ($30 cap on a 3 x $10 order)
    never fires — the line-qty guard is the one we're isolating here.
    """
    user = user_factory()
    admin = user_factory(email="admin-cumpick@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID, quantity=3)
    line = order.items[0]

    crud_refund.create_for_order(
        db_session,
        order=order,
        amount=20.0,  # 2 x 10
        reason=RefundReason.DEFECTIVE.value,
        note=None,
        restock=False,
        actor_user_id=admin.id,
        items=[(line.id, 2)],
    )
    with pytest.raises(RefundLineOverError):
        crud_refund.create_for_order(
            db_session,
            order=order,
            amount=5.0,  # well under the $10 remaining
            reason=RefundReason.DEFECTIVE.value,
            note=None,
            restock=False,
            actor_user_id=admin.id,
            items=[(line.id, 2)],  # cumulative qty = 4 > line.quantity (3)
        )


def test_restock_without_items_rejected(db_session, user_factory, product_factory):
    """D5: restock=True + items=None → RestockRequiresItemsError (nothing to put back)."""
    user = user_factory()
    admin = user_factory(email="admin-norestockpick@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID)

    with pytest.raises(RestockRequiresItemsError):
        crud_refund.create_for_order(
            db_session,
            order=order,
            amount=order.total,
            reason=RefundReason.DEFECTIVE.value,
            note=None,
            restock=True,
            actor_user_id=admin.id,
            items=None,
        )


def test_restock_with_empty_items_rejected(db_session, user_factory, product_factory):
    """An explicit empty list of items is the same as None for D5."""
    user = user_factory()
    admin = user_factory(email="admin-noempty@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID)

    with pytest.raises(RestockRequiresItemsError):
        crud_refund.create_for_order(
            db_session,
            order=order,
            amount=order.total,
            reason=RefundReason.DEFECTIVE.value,
            note=None,
            restock=True,
            actor_user_id=admin.id,
            items=[],
        )


def test_picked_items_persisted_in_refund_items(db_session, user_factory, product_factory):
    user = user_factory()
    admin = user_factory(email="admin-persist@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID, quantity=3)
    line = order.items[0]

    refund = crud_refund.create_for_order(
        db_session,
        order=order,
        amount=20.0,
        reason=RefundReason.WRONG_ITEM.value,
        note=None,
        restock=False,
        actor_user_id=admin.id,
        items=[(line.id, 2)],
    )
    assert len(refund.items) == 1
    assert refund.items[0].order_item_id == line.id
    assert refund.items[0].quantity == 2


def test_goodwill_refund_has_no_picker(db_session, user_factory, product_factory):
    """An ``items=None`` request records a refund row with no picker (goodwill credit)."""
    user = user_factory()
    admin = user_factory(email="admin-goodwill@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID)

    refund = crud_refund.create_for_order(
        db_session,
        order=order,
        amount=5.0,
        reason=RefundReason.CUSTOMER_REQUEST.value,
        note=None,
        restock=False,
        actor_user_id=admin.id,
        items=None,
    )
    assert refund.items == []


# ----------------------------------------------------------------------
# Stock restoration branch (D2/D8)
# ----------------------------------------------------------------------


def test_restock_false_writes_no_inventory_movement(db_session, user_factory, product_factory):
    user = user_factory()
    admin = user_factory(email="admin-norestock@example.com")
    product = product_factory(stock=3, price_usd=5.0)
    starting_stock = product.stock
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.SHIPPED)

    crud_refund.create_for_order(
        db_session,
        order=order,
        amount=order.total,
        reason=RefundReason.LOST_IN_TRANSIT.value,
        note="lost",
        restock=False,
        actor_user_id=admin.id,
    )

    # No movement was written for this product post-refund.
    refund_movements = (
        db_session.query(InventoryMovement)
        .filter(InventoryMovement.product_id == product.id)
        .filter(InventoryMovement.reason == "order_refunded")
        .all()
    )
    assert refund_movements == []
    # Stock unchanged (refund didn't touch it).
    db_session.refresh(product)
    assert product.stock == starting_stock


def test_restock_true_writes_one_movement_per_line(db_session, user_factory, product_factory):
    user = user_factory()
    admin = user_factory(email="admin-restock@example.com")
    product = product_factory(stock=5, price_usd=8.0)
    starting_stock = product.stock
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID, quantity=2)
    line = order.items[0]

    crud_refund.create_for_order(
        db_session,
        order=order,
        amount=order.total,
        reason=RefundReason.DEFECTIVE.value,
        note=None,
        restock=True,
        actor_user_id=admin.id,
        items=[(line.id, 2)],
    )

    movements = (
        db_session.query(InventoryMovement)
        .filter(InventoryMovement.product_id == product.id)
        .filter(InventoryMovement.reason == "order_refunded")
        .all()
    )
    assert len(movements) == 1, "one order_refunded movement per picked line"
    assert movements[0].delta == 2
    assert movements[0].actor_user_id == admin.id
    assert movements[0].order_id == order.id

    db_session.refresh(product)
    assert product.stock == starting_stock + 2


def test_restock_uses_picked_qty_not_line_qty(db_session, user_factory, product_factory):
    """Partial restock: 2-of-3 picked → +2 movement (NOT +3 from the original line qty)."""
    user = user_factory()
    admin = user_factory(email="admin-partialrestock@example.com")
    product = product_factory(stock=5, price_usd=8.0)
    starting_stock = product.stock
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID, quantity=3)
    line = order.items[0]

    crud_refund.create_for_order(
        db_session,
        order=order,
        amount=16.0,  # 2 x 8
        reason=RefundReason.DEFECTIVE.value,
        note=None,
        restock=True,
        actor_user_id=admin.id,
        items=[(line.id, 2)],
    )

    movements = (
        db_session.query(InventoryMovement)
        .filter(InventoryMovement.product_id == product.id)
        .filter(InventoryMovement.reason == "order_refunded")
        .all()
    )
    assert len(movements) == 1
    assert movements[0].delta == 2  # NOT 3 (the line qty)
    db_session.refresh(product)
    assert product.stock == starting_stock + 2


def test_restock_writes_movement_to_default_warehouse(db_session, user_factory, product_factory):
    """The order_refunded movement attributes to the default warehouse — same as the
    cancel path — so multi-warehouse setups don't have an orphan movement."""
    user = user_factory()
    admin = user_factory(email="admin-warehouse@example.com")
    product = product_factory(stock=4, price_usd=10.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID)
    line = order.items[0]

    crud_refund.create_for_order(
        db_session,
        order=order,
        amount=order.total,
        reason=RefundReason.WRONG_ITEM.value,
        note=None,
        restock=True,
        actor_user_id=admin.id,
        items=[(line.id, line.quantity)],
    )

    default = db_session.query(Warehouse).filter(Warehouse.is_default.is_(True)).one()
    movements = (
        db_session.query(InventoryMovement)
        .filter(InventoryMovement.reason == "order_refunded")
        .filter(InventoryMovement.order_id == order.id)
        .all()
    )
    assert all(m.warehouse_id == default.id for m in movements)


# ----------------------------------------------------------------------
# Faktura korygująca side effect (D6)
# ----------------------------------------------------------------------


def test_refund_issues_kor_when_order_has_faktura(db_session, user_factory, product_factory):
    user = user_factory()
    admin = user_factory(email="admin-kor@example.com")
    product = product_factory(stock=10, price_usd=15.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID)

    # Order has a faktura before the refund is issued (mirrors the pending→paid
    # flow that mark_paid runs).
    invoice = crud_invoice.issue_for_order(db_session, order=order)
    assert invoice.invoice_number

    refund = crud_refund.create_for_order(
        db_session,
        order=order,
        amount=order.total,
        reason=RefundReason.CUSTOMER_REQUEST.value,
        note=None,
        restock=False,
        actor_user_id=admin.id,
    )

    assert refund.corrective_invoice is not None
    assert refund.corrective_invoice.original_invoice_id == invoice.id
    assert refund.corrective_invoice.kor_number  # default format → "KOR/{year}/{seq:05d}"


def test_refund_without_faktura_skips_kor(db_session, user_factory, product_factory):
    """A refund against an order that has no faktura (rare — e.g. a manually-paid
    order that bypassed mark_paid) gets no KOR. The refund itself still stands."""
    user = user_factory()
    admin = user_factory(email="admin-nokor@example.com")
    product = product_factory(stock=10, price_usd=22.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID)
    # Deliberately NOT calling issue_for_order.

    refund = crud_refund.create_for_order(
        db_session,
        order=order,
        amount=order.total,
        reason=RefundReason.OTHER.value,
        note=None,
        restock=False,
        actor_user_id=admin.id,
    )

    assert refund.corrective_invoice is None


# ----------------------------------------------------------------------
# Read helpers
# ----------------------------------------------------------------------


def test_list_for_order_orders_ascending(db_session, user_factory, product_factory):
    """The schema technically allows multiple refunds per order (the v2 partial-
    refund PR will exercise that); the list must come back oldest-first so the
    running-total guard the v2 PR adds can iterate deterministically.

    v1 flips the order to ``refunded`` on the first refund, so to land a second
    refund we'd need to flip it back to ``paid`` mid-test — instead we just
    assert the list endpoint orders by ``created_at`` ASC for the single-refund
    case (the ORDER BY pins the contract even for the future case).
    """
    user = user_factory()
    admin = user_factory(email="admin-list@example.com")
    product = product_factory(stock=5, price_usd=7.0)
    order = _make_order(db_session, user=user, product=product, status=OrderStatus.PAID)
    crud_refund.create_for_order(
        db_session,
        order=order,
        amount=order.total,
        reason=RefundReason.CUSTOMER_REQUEST.value,
        note=None,
        restock=False,
        actor_user_id=admin.id,
    )

    rows = crud_refund.list_for_order(db_session, order_id=order.id)
    assert len(rows) == 1
    assert rows[0].order_id == order.id
