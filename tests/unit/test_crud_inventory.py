"""Unit tests for the inventory CRUD layer (DB-backed, no HTTP).

Covers ``apply_delta`` (mutation + ledger row + negative guard), ``status_for``
boundaries, the admin stock-list filters, and movement-log ordering.
"""

from __future__ import annotations

import pytest

from app.crud import inventory as crud_inventory
from app.models.inventory_movement import InventoryMovement, InventoryMovementReason

pytestmark = pytest.mark.integration


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
    assert [m.id for m, _ in rows] == [second.id, first.id]  # newest first (id desc tiebreak)
    assert all(name == product.name for _, name in rows)
    assert crud_inventory.count_movements(db_session, product_id=product.id) == 2
