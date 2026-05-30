"""Unit tests for the order CRUD layer (DB-backed, no HTTP).

Covers pricing/stock math in ``create_with_items``, stock restoration in
``cancel``, the scoped read helpers, and the ``status_transition_error`` map.
"""

from __future__ import annotations

import pytest

from app.crud import order as crud_order
from app.crud import warehouse as crud_warehouse
from app.crud.order import (
    InsufficientStockError,
    ProductUnavailableError,
    status_transition_error,
)
from app.models.inventory_movement import InventoryMovement
from app.models.order import OrderStatus
from app.models.user import UserRole
from app.schemas.order import OrderCreate, OrderItemCreate

pytestmark = pytest.mark.integration


def _order_in(
    items: list[tuple[int, int]],
    *,
    shipping_method_id: int,
    currency: str = "USD",
) -> OrderCreate:
    """Build an OrderCreate from ``(product_id, quantity)`` tuples.

    ``shipping_method_id`` is required (the validator now demands it); pass
    the ``test_shipping_method_id`` session fixture from the caller. Country
    is ISO-2 ``"PL"`` so the seeded Test Zone resolves a free rate.
    """
    return OrderCreate(
        items=[OrderItemCreate(product_id=pid, quantity=qty) for pid, qty in items],
        currency=currency,
        shipping_address="1 Test St",
        shipping_city="Testville",
        shipping_postal_code="00-001",
        shipping_country="PL",
        shipping_method_id=shipping_method_id,
    )


# ---------------------------------------------------------------------------
# create_with_items
# ---------------------------------------------------------------------------


def test_create_computes_totals_and_decrements_stock(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=20.0, price_pln=80.0)

    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_order_in([(product.id, 2)], shipping_method_id=test_shipping_method_id),
    )

    assert order.id is not None
    assert order.order_number.startswith("ORD-")
    assert order.status == OrderStatus.PENDING
    assert order.user_id == user.id
    assert order.subtotal == 40.0
    assert order.tax == 0.0
    assert order.shipping == 0.0
    assert order.total == 40.0
    assert order.currency == "USD"

    # Stock decremented on the product row.
    db_session.refresh(product)
    assert product.stock == 8

    # A single ORDER_PLACED inventory movement was written for the line,
    # attributed to the default warehouse (where orders are fulfilled from).
    default_warehouse = crud_warehouse.get_default(db_session)
    assert default_warehouse is not None
    movements = db_session.query(InventoryMovement).filter_by(product_id=product.id).all()
    assert len(movements) == 1
    assert movements[0].delta == -2
    assert movements[0].resulting_stock == 8
    assert movements[0].reason == "order_placed"
    assert movements[0].order_id == order.id
    assert movements[0].warehouse_id == default_warehouse.id

    # Line item snapshots product fields.
    assert len(order.items) == 1
    line = order.items[0]
    assert line.product_id == product.id
    assert line.product_name == product.name
    assert line.product_sku == product.sku
    assert line.quantity == 2
    assert line.unit_price == 20.0
    assert line.total_price == 40.0


def test_create_prices_in_requested_currency(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=5, price_usd=20.0, price_pln=80.0, price_eur=18.0)

    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_order_in(
            [(product.id, 1)], shipping_method_id=test_shipping_method_id, currency="PLN"
        ),
    )

    assert order.currency == "PLN"
    assert order.items[0].unit_price == 80.0
    assert order.total == 80.0


def test_create_multi_line_sums_subtotal(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    p1 = product_factory(stock=10, price_usd=10.0)
    p2 = product_factory(stock=10, price_usd=5.5)

    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_order_in([(p1.id, 2), (p2.id, 4)], shipping_method_id=test_shipping_method_id),
    )

    assert order.subtotal == 42.0  # 20 + 22
    assert order.total == 42.0
    assert {line.product_id for line in order.items} == {p1.id, p2.id}


def test_create_rejects_insufficient_stock(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=1, price_usd=20.0)

    with pytest.raises(InsufficientStockError) as exc:
        crud_order.create_with_items(
            db_session,
            user_id=user.id,
            obj_in=_order_in([(product.id, 3)], shipping_method_id=test_shipping_method_id),
        )
    assert exc.value.requested == 3
    assert exc.value.available == 1
    # Stock untouched on rejection.
    db_session.refresh(product)
    assert product.stock == 1


def test_create_allows_backorder_beyond_stock(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=2, allow_backorder=True, price_usd=10.0)

    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_order_in([(product.id, 5)], shipping_method_id=test_shipping_method_id),
    )
    db_session.refresh(product)

    # Stock went negative — the order is on backorder.
    assert product.stock == -3
    movements = db_session.query(InventoryMovement).filter_by(order_id=order.id).all()
    assert movements[0].resulting_stock == -3


def test_create_rejects_inactive_product(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=20.0, is_active=False)

    with pytest.raises(ProductUnavailableError):
        crud_order.create_with_items(
            db_session,
            user_id=user.id,
            obj_in=_order_in([(product.id, 1)], shipping_method_id=test_shipping_method_id),
        )


def test_create_rejects_missing_product(db_session, user_factory, test_shipping_method_id):
    user = user_factory()

    with pytest.raises(ProductUnavailableError):
        crud_order.create_with_items(
            db_session,
            user_id=user.id,
            obj_in=_order_in([(999_999, 1)], shipping_method_id=test_shipping_method_id),
        )


def test_create_duplicate_lines_check_running_stock(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=3, price_usd=10.0)

    # Two lines of 2 each = 4 > stock 3; the second line sees the decremented 1.
    with pytest.raises(InsufficientStockError):
        crud_order.create_with_items(
            db_session,
            user_id=user.id,
            obj_in=_order_in(
                [(product.id, 2), (product.id, 2)], shipping_method_id=test_shipping_method_id
            ),
        )


def test_create_multi_line_writes_one_movement_per_line(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    p1 = product_factory(stock=10, price_usd=10.0)
    p2 = product_factory(stock=5, price_usd=5.0)

    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_order_in([(p1.id, 2), (p2.id, 3)], shipping_method_id=test_shipping_method_id),
    )

    movements = (
        db_session.query(InventoryMovement).filter(InventoryMovement.order_id == order.id).all()
    )
    assert len(movements) == 2  # one per line, even across distinct products
    by_product = {m.product_id: m for m in movements}
    assert by_product[p1.id].delta == -2
    assert by_product[p1.id].resulting_stock == 8
    assert by_product[p2.id].delta == -3
    assert by_product[p2.id].resulting_stock == 2
    assert all(m.reason == "order_placed" for m in movements)


def test_create_duplicate_lines_that_fit_write_cumulative_movements(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=10.0)

    # Two lines of the same product (2 + 3 = 5 <= stock 10): one movement each,
    # with resulting_stock stepping down cumulatively (8 then 5).
    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_order_in(
            [(product.id, 2), (product.id, 3)], shipping_method_id=test_shipping_method_id
        ),
    )

    db_session.refresh(product)
    assert product.stock == 5

    movements = (
        db_session.query(InventoryMovement)
        .filter(InventoryMovement.order_id == order.id)
        .order_by(InventoryMovement.id)
        .all()
    )
    assert [m.delta for m in movements] == [-2, -3]
    assert [m.resulting_stock for m in movements] == [8, 5]


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def test_cancel_restores_stock(db_session, user_factory, product_factory, test_shipping_method_id):
    user = user_factory()
    product = product_factory(stock=10, price_usd=20.0)
    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_order_in([(product.id, 3)], shipping_method_id=test_shipping_method_id),
    )
    db_session.refresh(product)
    assert product.stock == 7

    crud_order.cancel(db_session, order=order)
    db_session.flush()

    assert order.status == OrderStatus.CANCELLED
    db_session.refresh(product)
    assert product.stock == 10

    # Place wrote one movement; cancel wrote a second (ORDER_CANCELLED, +3).
    movements = (
        db_session.query(InventoryMovement)
        .filter_by(product_id=product.id)
        .order_by(InventoryMovement.id)
        .all()
    )
    assert [m.reason for m in movements] == ["order_placed", "order_cancelled"]
    assert movements[-1].delta == 3
    assert movements[-1].resulting_stock == 10


def test_is_cancellable(db_session, user_factory, product_factory, test_shipping_method_id):
    user = user_factory()
    product = product_factory(stock=10, price_usd=20.0)
    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_order_in([(product.id, 1)], shipping_method_id=test_shipping_method_id),
    )
    assert crud_order.is_cancellable(order) is True
    order.status = OrderStatus.DELIVERED
    assert crud_order.is_cancellable(order) is False


# ---------------------------------------------------------------------------
# status_transition_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.PENDING, OrderStatus.PAID),
        (OrderStatus.PENDING, OrderStatus.PROCESSING),
        (OrderStatus.PAID, OrderStatus.SHIPPED),
        (OrderStatus.PROCESSING, OrderStatus.SHIPPED),
        (OrderStatus.SHIPPED, OrderStatus.DELIVERED),
        (OrderStatus.PAID, OrderStatus.PAID),  # no-op allowed
    ],
)
def test_transition_allowed(current, target):
    assert status_transition_error(current, target) is None


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.DELIVERED, OrderStatus.SHIPPED),  # backward
        (OrderStatus.PENDING, OrderStatus.DELIVERED),  # skips workflow
        (OrderStatus.DELIVERED, OrderStatus.PAID),  # terminal
    ],
)
def test_transition_rejected(current, target):
    assert status_transition_error(current, target) is not None


@pytest.mark.parametrize("target", [OrderStatus.CANCELLED, OrderStatus.REFUNDED])
def test_transition_to_cancelled_or_refunded_points_to_cancel_endpoint(target):
    msg = status_transition_error(OrderStatus.PENDING, target)
    assert msg is not None
    assert "cancel endpoint" in msg


# ---------------------------------------------------------------------------
# scoped reads
# ---------------------------------------------------------------------------


def test_get_scoped_customer_sees_only_own(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    owner = user_factory(email="owner@orders.test")
    other = user_factory(email="other@orders.test")
    product = product_factory(stock=10, price_usd=20.0)
    order = crud_order.create_with_items(
        db_session,
        user_id=owner.id,
        obj_in=_order_in([(product.id, 1)], shipping_method_id=test_shipping_method_id),
    )

    assert crud_order.get_scoped(db_session, order_id=order.id, user=owner) is not None
    assert crud_order.get_scoped(db_session, order_id=order.id, user=other) is None


def test_get_scoped_admin_sees_any(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    owner = user_factory(email="owner2@orders.test")
    admin = user_factory(email="admin2@orders.test", role=UserRole.ADMIN)
    product = product_factory(stock=10, price_usd=20.0)
    order = crud_order.create_with_items(
        db_session,
        user_id=owner.id,
        obj_in=_order_in([(product.id, 1)], shipping_method_id=test_shipping_method_id),
    )

    assert crud_order.get_scoped(db_session, order_id=order.id, user=admin) is not None


def test_multi_scoped_and_count_filter(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    owner = user_factory(email="owner3@orders.test")
    other = user_factory(email="other3@orders.test")
    admin = user_factory(email="admin3@orders.test", role=UserRole.ADMIN)
    product = product_factory(stock=50, price_usd=10.0)

    o1 = crud_order.create_with_items(
        db_session,
        user_id=owner.id,
        obj_in=_order_in([(product.id, 1)], shipping_method_id=test_shipping_method_id),
    )
    crud_order.create_with_items(
        db_session,
        user_id=other.id,
        obj_in=_order_in([(product.id, 1)], shipping_method_id=test_shipping_method_id),
    )

    # Customer sees only own.
    assert crud_order.count_scoped(db_session, user=owner) == 1
    assert [o.id for o in crud_order.get_multi_scoped(db_session, user=owner)] == [o1.id]

    # Admin sees all.
    assert crud_order.count_scoped(db_session, user=admin) == 2

    # Status filter.
    crud_order.cancel(db_session, order=o1)
    db_session.flush()
    assert crud_order.count_scoped(db_session, user=admin, status=OrderStatus.CANCELLED) == 1
    assert crud_order.count_scoped(db_session, user=admin, status=OrderStatus.PENDING) == 1
