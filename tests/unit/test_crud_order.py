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
    is_inpost_locker_method,
    status_transition_error,
)
from app.models.inventory_movement import InventoryMovement
from app.models.order import OrderStatus
from app.models.product import Product
from app.models.shipping import ShippingMethod
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


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("inpost_paczkomat", True),
        ("INPOST_LOCKER", True),
        ("inpost", True),
        ("dpd_classic", False),
        ("standard", False),
        (None, False),
    ],
)
def test_is_inpost_locker_method(code, expected):
    assert is_inpost_locker_method(ShippingMethod(name="m", code=code)) is expected


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
    # D4: once any refund has issued, cancel is not allowed — the only forward
    # path is another refund (which may flip the status to REFUNDED).
    order.status = OrderStatus.PARTIALLY_REFUNDED
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


def test_transition_to_cancelled_points_to_cancel_endpoint():
    msg = status_transition_error(OrderStatus.PENDING, OrderStatus.CANCELLED)
    assert msg is not None
    assert "cancel endpoint" in msg


def test_transition_to_refunded_points_to_refund_endpoint():
    """The PATCH path keeps rejecting refunded — the admin must use POST .../refunds
    (mirrors the cancel-endpoint pattern). Updated 2026-05-30 when the dedicated
    refund endpoint shipped."""
    msg = status_transition_error(OrderStatus.PAID, OrderStatus.REFUNDED)
    assert msg is not None
    assert "refunds" in msg
    assert "POST" in msg


def test_transition_to_partially_refunded_points_to_refund_endpoint():
    """v2: PATCH ``status=partially_refunded`` is rejected with the same hint
    as REFUNDED — the dedicated refund endpoint owns both refund-status flips."""
    msg = status_transition_error(OrderStatus.PAID, OrderStatus.PARTIALLY_REFUNDED)
    assert msg is not None
    assert "refunds" in msg
    assert "POST" in msg


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


# ---------------------------------------------------------------------------
# Variant lines (PR-B2) — create / cancel / FK-ordering
# ---------------------------------------------------------------------------


def _add_variants(db_session, product, *, stocks=(4, 6), price_usd=(None, None)):
    """Author a one-option (Red|Blue) variant matrix on ``product``.

    Returns the two variants in position order. ``price_usd=None`` keeps the
    inherit-from-product behavior; stocks are seeded through the ledger.
    """
    from app.crud import product_variant as crud_product_variant
    from app.schemas.product_variant import ProductOptionsMatrixUpdate

    matrix = ProductOptionsMatrixUpdate.model_validate(
        {
            "options": [
                {
                    "name": "Color",
                    "position": 0,
                    "values": [
                        {"value": "Red", "position": 0},
                        {"value": "Blue", "position": 1},
                    ],
                }
            ],
            "variants": [
                {
                    "option_values": ["Red"],
                    "sku": f"VO-RED-{product.id}",
                    "initial_stock": stocks[0],
                    "price_usd": price_usd[0],
                    "position": 0,
                },
                {
                    "option_values": ["Blue"],
                    "sku": f"VO-BLUE-{product.id}",
                    "initial_stock": stocks[1],
                    "price_usd": price_usd[1],
                    "position": 1,
                },
            ],
        }
    )
    crud_product_variant.apply_matrix(db_session, product=product, matrix=matrix)
    db_session.commit()
    db_session.refresh(product)
    return crud_product_variant.get_for_product(db_session, product_id=product.id)


def _variant_order_in(
    items: list[tuple[int, int | None, int]],
    *,
    shipping_method_id: int,
    currency: str = "USD",
) -> OrderCreate:
    """Build an OrderCreate from ``(product_id, variant_id, quantity)`` tuples."""
    return OrderCreate(
        items=[
            OrderItemCreate(product_id=pid, variant_id=vid, quantity=qty) for pid, vid, qty in items
        ],
        currency=currency,
        shipping_address="1 Test St",
        shipping_city="Testville",
        shipping_postal_code="00-001",
        shipping_country="PL",
        shipping_method_id=shipping_method_id,
    )


def test_create_variant_line_snapshots_and_decrements_at_variant_grain(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=0, price_usd=20.0)
    red, blue = _add_variants(db_session, product, price_usd=(25.0, None))

    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_variant_order_in(
            [(product.id, red.id, 2)], shipping_method_id=test_shipping_method_id
        ),
    )

    # Snapshot carries the variant ref + title + the VARIANT sku.
    line = order.items[0]
    assert line.variant_id == red.id
    assert line.variant_title == "Red"
    assert line.product_sku == f"VO-RED-{product.id}"
    assert line.product_name == product.name
    # Variant price override used (not the product's 20.0).
    assert line.unit_price == 25.0
    assert order.subtotal == 50.0

    # All three aggregates moved in lock-step at variant grain.
    db_session.refresh(product)
    db_session.refresh(red)
    db_session.refresh(blue)
    assert red.stock == 2
    assert blue.stock == 6  # untouched
    assert product.stock == 8

    movements = db_session.query(InventoryMovement).filter_by(order_id=order.id).all()
    assert len(movements) == 1
    assert movements[0].variant_id == red.id
    assert movements[0].delta == -2
    assert movements[0].resulting_stock == 2


def test_create_variant_line_inherits_product_price_on_null(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=0, price_usd=20.0, price_pln=80.0)
    red, _blue = _add_variants(db_session, product)  # full-NULL prices

    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_variant_order_in(
            [(product.id, red.id, 1)],
            shipping_method_id=test_shipping_method_id,
            currency="PLN",
        ),
    )

    assert order.items[0].unit_price == 80.0
    assert order.total == 80.0


def test_create_variant_line_falls_back_to_product_sku_when_variant_sku_null(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    from app.crud import product_variant as crud_product_variant
    from app.schemas.product_variant import ProductVariantUpdate

    user = user_factory()
    product = product_factory(stock=0, price_usd=20.0)
    red, _blue = _add_variants(db_session, product)
    crud_product_variant.update_variant(
        db_session, variant=red, obj_in=ProductVariantUpdate(sku=None)
    )
    db_session.commit()

    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_variant_order_in(
            [(product.id, red.id, 1)], shipping_method_id=test_shipping_method_id
        ),
    )
    assert order.items[0].product_sku == product.sku


def test_create_requires_variant_for_variant_product(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    from app.crud.order import VariantRequiredError

    user = user_factory()
    product = product_factory(stock=0, price_usd=20.0)
    _add_variants(db_session, product)

    with pytest.raises(VariantRequiredError) as exc:
        crud_order.create_with_items(
            db_session,
            user_id=user.id,
            obj_in=_order_in([(product.id, 1)], shipping_method_id=test_shipping_method_id),
        )
    assert exc.value.product_id == product.id


def test_create_plain_line_ok_when_all_variants_inactive(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    """A product whose variants are ALL inactive behaves variantless at
    order time (``has_variants`` derives from *active* rows)."""
    from app.crud import product_variant as crud_product_variant
    from app.schemas.product_variant import ProductVariantUpdate

    user = user_factory()
    product = product_factory(stock=0, price_usd=20.0, allow_backorder=True)
    for variant in _add_variants(db_session, product, stocks=(0, 0)):
        crud_product_variant.update_variant(
            db_session, variant=variant, obj_in=ProductVariantUpdate(is_active=False)
        )
    db_session.commit()

    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_order_in([(product.id, 1)], shipping_method_id=test_shipping_method_id),
    )
    assert order.items[0].variant_id is None
    assert order.items[0].variant_title is None


@pytest.mark.parametrize("case", ["missing", "inactive", "foreign"])
def test_create_rejects_unavailable_variant(
    db_session, user_factory, product_factory, test_shipping_method_id, case
):
    from app.crud import product_variant as crud_product_variant
    from app.crud.order import VariantUnavailableError
    from app.schemas.product_variant import ProductVariantUpdate

    user = user_factory()
    product = product_factory(stock=0, price_usd=20.0)
    red, _blue = _add_variants(db_session, product)

    if case == "missing":
        variant_id = 999_999
    elif case == "inactive":
        crud_product_variant.update_variant(
            db_session, variant=red, obj_in=ProductVariantUpdate(is_active=False)
        )
        db_session.commit()
        variant_id = red.id
    else:  # foreign — another product's variant
        other = product_factory(stock=0, price_usd=10.0)
        other_red, _ = _add_variants(db_session, other)
        variant_id = other_red.id

    with pytest.raises(VariantUnavailableError) as exc:
        crud_order.create_with_items(
            db_session,
            user_id=user.id,
            obj_in=_variant_order_in(
                [(product.id, variant_id, 1)], shipping_method_id=test_shipping_method_id
            ),
        )
    assert exc.value.variant_id == variant_id


def test_create_duplicate_variant_lines_check_running_remainder(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    """Duplicate (product, variant) lines validate against the running
    remainder; distinct variants of the same product do not interfere."""
    user = user_factory()
    product = product_factory(stock=0, price_usd=20.0)
    red, blue = _add_variants(db_session, product, stocks=(3, 5))

    # 2 + 2 = 4 > red's 3 — second line sees the decremented remainder.
    with pytest.raises(InsufficientStockError):
        crud_order.create_with_items(
            db_session,
            user_id=user.id,
            obj_in=_variant_order_in(
                [(product.id, red.id, 2), (product.id, red.id, 2)],
                shipping_method_id=test_shipping_method_id,
            ),
        )

    # Red 3 + Blue 5 across distinct variants fits even though either alone
    # exceeds the other's level.
    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_variant_order_in(
            [(product.id, red.id, 3), (product.id, blue.id, 5)],
            shipping_method_id=test_shipping_method_id,
        ),
    )
    db_session.refresh(product)
    assert product.stock == 0
    movements = db_session.query(InventoryMovement).filter_by(order_id=order.id).all()
    assert {(m.variant_id, m.delta) for m in movements} == {(red.id, -3), (blue.id, -5)}


def test_create_variant_with_unmaterialised_level_reads_zero(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    """§7 risk 3: a variant whose level was never materialised (no initial
    stock seed) reads 0 — no unallocated synthesis at variant grain."""
    user = user_factory()
    # Product row carries stock the variant never claimed — the variant-grain
    # check must NOT see it.
    product = product_factory(stock=50, price_usd=20.0)
    red, _blue = _add_variants(db_session, product, stocks=(0, 0))

    with pytest.raises(InsufficientStockError) as exc:
        crud_order.create_with_items(
            db_session,
            user_id=user.id,
            obj_in=_variant_order_in(
                [(product.id, red.id, 1)], shipping_method_id=test_shipping_method_id
            ),
        )
    assert exc.value.available == 0


def test_cancel_restores_stock_at_variant_grain(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=0, price_usd=20.0)
    red, _blue = _add_variants(db_session, product)

    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_variant_order_in(
            [(product.id, red.id, 3)], shipping_method_id=test_shipping_method_id
        ),
    )
    db_session.refresh(red)
    assert red.stock == 1

    crud_order.cancel(db_session, order=order)
    db_session.flush()

    db_session.refresh(red)
    db_session.refresh(product)
    assert red.stock == 4
    assert product.stock == 10
    restore = (
        db_session.query(InventoryMovement)
        .filter_by(order_id=order.id, reason="order_cancelled")
        .one()
    )
    assert restore.variant_id == red.id
    assert restore.delta == 3
    assert restore.note is None


def test_cancel_deleted_variant_restores_at_product_grain_with_note(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    from app.crud import product_variant as crud_product_variant

    user = user_factory()
    product = product_factory(stock=0, price_usd=20.0)
    red, _blue = _add_variants(db_session, product)

    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_variant_order_in(
            [(product.id, red.id, 2)], shipping_method_id=test_shipping_method_id
        ),
    )
    db_session.flush()

    # Delete the variant — order_items.variant_id is SET NULL, the title
    # snapshot survives.
    crud_product_variant.delete_variant(db_session, product=product, variant=red)
    db_session.commit()
    db_session.refresh(order)
    line = order.items[0]
    assert line.variant_id is None
    assert line.variant_title == "Red"

    db_session.refresh(product)
    stock_before = product.stock

    crud_order.cancel(db_session, order=order)
    db_session.flush()
    db_session.refresh(product)

    # Restored at PRODUCT grain (variant_id NULL on the movement) with a note.
    assert product.stock == stock_before + 2
    restore = (
        db_session.query(InventoryMovement)
        .filter_by(order_id=order.id, reason="order_cancelled")
        .one()
    )
    assert restore.variant_id is None
    assert restore.note is not None
    assert "Red" in restore.note


def test_product_delete_cascades_variants_and_levels(db_session, product_factory):
    """§7 risk 11 (no-orders case): deleting a variant product CASCADEs its
    options, variants and their stock levels without FK-ordering errors."""
    from app.models.product_variant import ProductOption, ProductVariant
    from app.models.warehouse import StockLevel

    product = product_factory(stock=0)
    _add_variants(db_session, product)
    product_id = product.id

    db_session.delete(product)
    db_session.commit()

    assert db_session.query(ProductVariant).filter_by(product_id=product_id).count() == 0
    assert db_session.query(ProductOption).filter_by(product_id=product_id).count() == 0
    assert db_session.query(StockLevel).filter_by(product_id=product_id).count() == 0


def test_variant_delete_set_nulls_order_item_ref_keeps_snapshot(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    """§7 risk 11 (ordered case): order_items.product_id is a plain NO-ACTION
    FK, so an *ordered* product is not deletable (unchanged, pre-variants
    behavior) — the SET-NULL ordering that matters fires on VARIANT deletion:
    the line keeps its snapshots while the ref nulls out."""
    import sqlalchemy.exc

    from app.crud import product_variant as crud_product_variant
    from app.models.order import OrderItem

    user = user_factory()
    product = product_factory(stock=0, price_usd=20.0)
    red, _blue = _add_variants(db_session, product)
    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_variant_order_in(
            [(product.id, red.id, 1)], shipping_method_id=test_shipping_method_id
        ),
    )
    db_session.commit()

    # The ordered product itself stays undeletable (order_items.product_id FK).
    db_session.delete(product)
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        db_session.commit()
    db_session.rollback()

    # Deleting just the variant SET-NULLs the line ref; snapshots survive.
    product = db_session.get(Product, order.items[0].product_id)
    assert product is not None
    line_variant_id = order.items[0].variant_id
    assert line_variant_id is not None
    red = crud_product_variant.get(db_session, product_id=product.id, variant_id=line_variant_id)
    assert red is not None
    crud_product_variant.delete_variant(db_session, product=product, variant=red)
    db_session.commit()

    line = db_session.query(OrderItem).filter_by(order_id=order.id).one()
    assert line.variant_id is None
    assert line.variant_title == "Red"
    assert line.product_sku == f"VO-RED-{product.id}"
