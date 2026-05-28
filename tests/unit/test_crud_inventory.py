"""Unit tests for the inventory CRUD layer (DB-backed, no HTTP).

Covers ``apply_delta`` (mutation + ledger row + negative guard), ``status_for``
boundaries, the admin stock-list filters, and movement-log ordering.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func

from app.crud import inventory as crud_inventory
from app.models.inventory_movement import InventoryMovement, InventoryMovementReason
from app.models.warehouse import StockLevel, Warehouse

pytestmark = pytest.mark.integration


def _make_warehouse(db_session, code: str) -> Warehouse:
    wh = Warehouse(name=code, code=code, is_active=True, is_default=False)
    db_session.add(wh)
    db_session.flush()
    return wh


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
