"""Unit tests for the inventory CRUD layer (DB-backed, no HTTP).

Covers ``apply_delta`` (mutation + ledger row + negative guard), ``status_for``
boundaries, the admin stock-list filters, and movement-log ordering.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.crud import inventory as crud_inventory
from app.crud import warehouse as crud_warehouse
from app.models.inventory_movement import InventoryMovement, InventoryMovementReason
from app.models.product_variant import ProductOption, ProductOptionValue, ProductVariant
from app.models.warehouse import StockLevel, Warehouse

pytestmark = pytest.mark.integration


def _make_warehouse(db_session, code: str) -> Warehouse:
    wh = Warehouse(name=code, code=code, is_active=True, is_default=False)
    db_session.add(wh)
    db_session.flush()
    return wh


def _make_variant(db_session, product, value: str = "Red") -> ProductVariant:
    """Materialise a minimal one-option variant directly (no matrix machinery)."""
    option = ProductOption(product_id=product.id, name="Color", position=0)
    option.values.append(ProductOptionValue(value=value, position=0))
    db_session.add(option)
    db_session.flush()
    variant = ProductVariant(
        product_id=product.id,
        title=value,
        option_signature=str(option.values[0].id),
        stock=0,
    )
    variant.option_values = [option.values[0]]
    db_session.add(variant)
    db_session.flush()
    return variant


# ---------------------------------------------------------------------------
# apply_delta
# ---------------------------------------------------------------------------


def test_apply_delta_decrements_and_records(db_session, product_factory):
    product = product_factory(stock=10)

    movement = crud_inventory.apply_delta(
        db_session,
        product=product,
        delta=-3,
        reason=InventoryMovementReason.MANUAL_ADJUSTMENT,
        note="recount",
    )
    db_session.flush()

    assert product.stock == 7
    assert movement.delta == -3
    assert movement.resulting_stock == 7
    assert movement.reason == "manual_adjustment"
    assert movement.note == "recount"

    rows = db_session.query(InventoryMovement).filter_by(product_id=product.id).all()
    assert len(rows) == 1


def test_apply_delta_increments(db_session, product_factory):
    product = product_factory(stock=5)

    movement = crud_inventory.apply_delta(
        db_session, product=product, delta=4, reason=InventoryMovementReason.ORDER_CANCELLED
    )
    db_session.flush()

    assert product.stock == 9
    assert movement.delta == 4
    assert movement.resulting_stock == 9
    assert movement.reason == "order_cancelled"


def test_apply_delta_rejects_negative_result(db_session, product_factory):
    product = product_factory(stock=2)

    with pytest.raises(ValueError, match="negative"):
        crud_inventory.apply_delta(
            db_session, product=product, delta=-5, reason=InventoryMovementReason.MANUAL_ADJUSTMENT
        )

    # The guard runs before the mutation, so stock and the ledger are untouched.
    assert product.stock == 2
    assert db_session.query(InventoryMovement).filter_by(product_id=product.id).count() == 0


# ---------------------------------------------------------------------------
# apply_delta — multi-warehouse
# ---------------------------------------------------------------------------


def test_apply_delta_defaults_to_default_warehouse_and_seeds_from_stock(
    db_session, product_factory
):
    """A factory product has stock but no materialised level; the first default
    apply_delta seeds the default level from product.stock, so the movement's
    per-warehouse resulting_stock equals the total."""
    product = product_factory(stock=10)
    movement = crud_inventory.apply_delta(
        db_session, product=product, delta=-3, reason=InventoryMovementReason.ORDER_PLACED
    )
    db_session.flush()

    assert movement.warehouse_id is not None
    assert movement.resulting_stock == 7
    assert product.stock == 7


def test_apply_delta_targets_explicit_warehouse_and_maintains_total(db_session, product_factory):
    """Moving stock from default into a second warehouse keeps the total invariant
    Product.stock == sum(StockLevel.quantity)."""
    product = product_factory(stock=10)
    eu = _make_warehouse(db_session, "EU-T2")

    # Materialise + decrement the default, then add the same amount to EU.
    crud_inventory.apply_delta(
        db_session, product=product, delta=-3, reason=InventoryMovementReason.MANUAL_ADJUSTMENT
    )
    moved = crud_inventory.apply_delta(
        db_session,
        product=product,
        delta=3,
        reason=InventoryMovementReason.MANUAL_ADJUSTMENT,
        warehouse_id=eu.id,
    )
    db_session.flush()

    assert moved.warehouse_id == eu.id
    assert moved.resulting_stock == 3  # EU seeded at 0, then +3
    assert product.stock == 10  # net unchanged
    total_levels = (
        db_session.query(func.coalesce(func.sum(StockLevel.quantity), 0))
        .filter(StockLevel.product_id == product.id)
        .scalar()
    )
    assert total_levels == 10  # invariant: total == sum of levels


def test_apply_delta_non_default_warehouse_seeds_zero(db_session, product_factory):
    """A non-default warehouse with no prior level seeds at 0, so a decrement
    there fails even though the product has stock in the default warehouse."""
    product = product_factory(stock=10)
    eu = _make_warehouse(db_session, "EU-T3")

    with pytest.raises(ValueError, match="negative"):
        crud_inventory.apply_delta(
            db_session,
            product=product,
            delta=-1,
            reason=InventoryMovementReason.ORDER_PLACED,
            warehouse_id=eu.id,
        )


def test_apply_delta_allow_negative_permits_backorder(db_session, product_factory):
    """allow_negative lets a level (and the total) go below zero — the backorder
    path. resulting_stock reflects the negative level."""
    product = product_factory(stock=2)
    movement = crud_inventory.apply_delta(
        db_session,
        product=product,
        delta=-5,
        reason=InventoryMovementReason.ORDER_PLACED,
        allow_negative=True,
    )
    db_session.flush()

    assert movement.resulting_stock == -3
    assert product.stock == -3


# ---------------------------------------------------------------------------
# apply_delta — variant grain
# ---------------------------------------------------------------------------


def test_apply_delta_variant_maintains_all_three_aggregates(db_session, product_factory):
    """The variant branch keeps the (level, variant.stock, product.stock)
    triple in lock-step."""
    product = product_factory(stock=0)
    variant = _make_variant(db_session, product)

    movement = crud_inventory.apply_delta(
        db_session,
        product=product,
        delta=5,
        reason=InventoryMovementReason.RESTOCK,
        variant=variant,
    )
    db_session.flush()

    assert movement.variant_id == variant.id
    assert movement.resulting_stock == 5
    assert variant.stock == 5
    assert product.stock == 5
    level = (
        db_session.query(StockLevel)
        .filter(StockLevel.product_id == product.id, StockLevel.variant_id == variant.id)
        .one()
    )
    assert level.quantity == 5

    crud_inventory.apply_delta(
        db_session,
        product=product,
        delta=-2,
        reason=InventoryMovementReason.MANUAL_ADJUSTMENT,
        variant=variant,
    )
    db_session.flush()
    assert variant.stock == 3
    assert product.stock == 3
    assert level.quantity == 3


def test_apply_delta_variant_level_seeds_zero_never_unallocated(db_session, product_factory):
    """A variant-grain level never seeds from the product's unallocated stock —
    a decrement against an unmaterialised variant level fails even though the
    product has (product-grain) stock."""
    product = product_factory(stock=10)
    variant = _make_variant(db_session, product)

    with pytest.raises(ValueError, match="negative"):
        crud_inventory.apply_delta(
            db_session,
            product=product,
            delta=-1,
            reason=InventoryMovementReason.ORDER_PLACED,
            variant=variant,
        )


def test_get_level_quantity_variant_missing_row_is_zero(db_session, product_factory):
    """No synthesis at variant grain: a missing level reads 0 (the order-create
    guard for a variant whose level was never materialised)."""
    product = product_factory(stock=10)
    variant = _make_variant(db_session, product)
    default = crud_warehouse.get_default(db_session)
    assert default is not None

    assert (
        crud_inventory.get_level_quantity(
            db_session, product=product, warehouse_id=default.id, variant=variant
        )
        == 0
    )
    # The product-grain read still synthesises unallocated default stock.
    assert (
        crud_inventory.get_level_quantity(db_session, product=product, warehouse_id=default.id)
        == 10
    )


def test_default_unallocated_excludes_variant_stock(db_session, product_factory):
    """Variant stock is never 'unallocated' — once all stock lives at variant
    grain, the product-grain default read reports 0."""
    product = product_factory(stock=0)
    variant = _make_variant(db_session, product)
    crud_inventory.apply_delta(
        db_session,
        product=product,
        delta=8,
        reason=InventoryMovementReason.RESTOCK,
        variant=variant,
    )
    db_session.flush()
    default = crud_warehouse.get_default(db_session)
    assert default is not None

    # Remove the product-grain NULL row if any was materialised (none should be).
    assert (
        crud_inventory.get_level_quantity(db_session, product=product, warehouse_id=default.id) == 0
    )


def test_stock_levels_nulls_not_distinct_unique(db_session, product_factory):
    """PG16 NULLS NOT DISTINCT: two NULL-variant rows for the same (product,
    warehouse) collide like any other duplicate."""
    product = product_factory(stock=0)
    default = crud_warehouse.get_default(db_session)
    assert default is not None

    db_session.add(
        StockLevel(product_id=product.id, warehouse_id=default.id, variant_id=None, quantity=1)
    )
    db_session.flush()
    db_session.add(
        StockLevel(product_id=product.id, warehouse_id=default.id, variant_id=None, quantity=2)
    )
    with pytest.raises(IntegrityError, match="uq_stock_levels_product_warehouse_variant"):
        db_session.flush()
    db_session.rollback()


# ---------------------------------------------------------------------------
# status_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stock", "threshold", "expected"),
    [
        (0, None, "out_of_stock"),
        (0, 5, "out_of_stock"),
        (3, 5, "low_stock"),
        (5, 5, "low_stock"),  # at the threshold is still "low"
        (6, 5, "in_stock"),
        (10, None, "in_stock"),  # null threshold → never low
        (1, 0, "in_stock"),  # threshold 0 → positive stock never low
    ],
)
def test_status_for(db_session, product_factory, stock, threshold, expected):
    product = product_factory(stock=stock, low_stock_threshold=threshold)
    assert crud_inventory.status_for(product) == expected


def test_status_for_backorder(db_session, product_factory):
    """At/below zero stock, a backorder-enabled product is ``backorder``, not ``out_of_stock``."""
    assert crud_inventory.status_for(product_factory(stock=0, allow_backorder=True)) == "backorder"
    assert (
        crud_inventory.status_for(product_factory(stock=0, allow_backorder=False)) == "out_of_stock"
    )


# ---------------------------------------------------------------------------
# admin list filters
# ---------------------------------------------------------------------------


def test_get_multi_admin_status_filter(db_session, product_factory):
    out = product_factory(stock=0)
    low = product_factory(stock=2, low_stock_threshold=5)
    in_high = product_factory(stock=50, low_stock_threshold=5)
    in_no_threshold = product_factory(stock=1)  # threshold None → in_stock

    assert {p.id for p in crud_inventory.get_multi_admin(db_session, status="out_of_stock")} == {
        out.id
    }
    assert {p.id for p in crud_inventory.get_multi_admin(db_session, status="low_stock")} == {
        low.id
    }
    assert {p.id for p in crud_inventory.get_multi_admin(db_session, status="in_stock")} == {
        in_high.id,
        in_no_threshold.id,
    }

    assert crud_inventory.count_admin(db_session, status="out_of_stock") == 1
    assert crud_inventory.count_admin(db_session) == 4


def test_get_multi_admin_search(db_session, product_factory):
    shirt = product_factory(name="Blue Shirt", sku="BLU-001")
    hat = product_factory(name="Red Hat", sku="RED-001")

    assert {p.id for p in crud_inventory.get_multi_admin(db_session, search="shirt")} == {shirt.id}
    assert {p.id for p in crud_inventory.get_multi_admin(db_session, search="RED-")} == {hat.id}


# ---------------------------------------------------------------------------
# movement log
# ---------------------------------------------------------------------------


def test_movements_for_newest_first_with_product_name(db_session, product_factory):
    product = product_factory(stock=10)
    first = crud_inventory.apply_delta(
        db_session, product=product, delta=-1, reason=InventoryMovementReason.MANUAL_ADJUSTMENT
    )
    second = crud_inventory.apply_delta(
        db_session, product=product, delta=-1, reason=InventoryMovementReason.MANUAL_ADJUSTMENT
    )
    db_session.flush()

    rows = crud_inventory.movements_for(db_session, product_id=product.id)
    assert [m.id for m, _, _ in rows] == [second.id, first.id]  # newest first (id desc tiebreak)
    assert all(pname == product.name for _, pname, _ in rows)
    # movements default to the default warehouse, so the joined name is populated.
    assert all(wname is not None for _, _, wname in rows)
    assert crud_inventory.count_movements(db_session, product_id=product.id) == 2
