"""Unit tests for the option/variant CRUD layer (DB-backed, no HTTP).

Covers the matrix reconcile (create/update/delete diff), the option/variant
caps, duplicate-combination rejection, ``get_price`` inheritance, and the §0
stock-transition rules (product-grain zero-out + variant seeding through the
inventory ledger).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.crud import product_variant as crud_product_variant
from app.crud.product_variant import VariantMatrixError, VariantSkuConflictError
from app.models.inventory_movement import InventoryMovement
from app.models.product_variant import ProductOptionValue, ProductVariant
from app.models.warehouse import StockLevel
from app.schemas.product_variant import (
    ProductOptionInput,
    ProductOptionsMatrixUpdate,
    ProductOptionValueInput,
    ProductVariantInput,
)

pytestmark = pytest.mark.integration


def _matrix(options: list[dict], variants: list[dict]) -> ProductOptionsMatrixUpdate:
    """Build a validated matrix body from plain dicts."""
    return ProductOptionsMatrixUpdate.model_validate({"options": options, "variants": variants})


def _color_size_matrix(**variant_overrides) -> ProductOptionsMatrixUpdate:
    """A 2x1 Color/Size matrix (Red|Blue x XL) with optional variant tweaks."""
    base = [
        {"option_values": ["Red", "XL"], "sku": "VAR-RED-XL", "initial_stock": 4},
        {"option_values": ["Blue", "XL"], "sku": "VAR-BLUE-XL", "initial_stock": 6},
    ]
    for entry in base:
        entry.update(variant_overrides)
    return _matrix(
        options=[
            {
                "name": "Color",
                "name_pl": "Kolor",
                "position": 0,
                "values": [
                    {"value": "Red", "value_pl": "Czerwony", "position": 0},
                    {"value": "Blue", "value_pl": "Niebieski", "position": 1},
                ],
            },
            {"name": "Size", "position": 1, "values": [{"value": "XL", "position": 0}]},
        ],
        variants=base,
    )


# ---------------------------------------------------------------------------
# Matrix reconcile — create
# ---------------------------------------------------------------------------


def test_apply_matrix_creates_options_values_and_variants(db_session, product_factory):
    product = product_factory(stock=0)

    result = crud_product_variant.apply_matrix(
        db_session, product=product, matrix=_color_size_matrix()
    )
    db_session.commit()
    db_session.refresh(product)

    assert [option.name for option in product.options] == ["Color", "Size"]
    assert [value.value for value in product.options[0].values] == ["Red", "Blue"]
    assert product.options[0].name_pl == "Kolor"
    assert len(result.created_ids) == 2
    assert result.updated_ids == []
    assert result.deleted_ids == []

    variants = crud_product_variant.get_for_product(db_session, product_id=product.id)
    assert [variant.title for variant in variants] == ["Red / XL", "Blue / XL"]
    # PL title falls back to EN per value ("XL" has no value_pl).
    assert variants[0].title_pl == "Czerwony / XL"
    # Signature is the sorted value ids joined with "-".
    red_xl_ids = sorted(value.id for value in variants[0].option_values)
    assert variants[0].option_signature == "-".join(str(i) for i in red_xl_ids)


def test_apply_matrix_seeds_initial_stock_through_the_ledger(db_session, product_factory):
    product = product_factory(stock=0)

    crud_product_variant.apply_matrix(db_session, product=product, matrix=_color_size_matrix())
    db_session.commit()
    db_session.refresh(product)

    variants = crud_product_variant.get_for_product(db_session, product_id=product.id)
    assert [variant.stock for variant in variants] == [4, 6]
    assert product.stock == 10  # invariant: product.stock == sum(variant.stock)

    # Each seed is a restock movement at variant grain; levels are materialised.
    movements = (
        db_session.query(InventoryMovement).filter_by(product_id=product.id, reason="restock").all()
    )
    assert {(m.variant_id, m.delta) for m in movements} == {
        (variants[0].id, 4),
        (variants[1].id, 6),
    }
    levels = db_session.query(StockLevel).filter_by(product_id=product.id).all()
    assert {(lvl.variant_id, lvl.quantity) for lvl in levels} == {
        (variants[0].id, 4),
        (variants[1].id, 6),
    }


def test_apply_matrix_first_variants_zero_out_product_grain_stock(db_session, product_factory):
    """§0 transition: existing product-grain stock is zeroed via apply_delta
    (ledger row 'migrated to variants'), then variant stock is seeded."""
    product = product_factory(stock=7)

    crud_product_variant.apply_matrix(db_session, product=product, matrix=_color_size_matrix())
    db_session.commit()
    db_session.refresh(product)

    assert product.stock == 10  # 4 + 6 from the seeds; the old 7 was zeroed
    migration = (
        db_session.query(InventoryMovement)
        .filter_by(product_id=product.id, reason="manual_adjustment")
        .all()
    )
    assert [(m.delta, m.note, m.variant_id) for m in migration] == [
        (-7, "migrated to variants", None)
    ]
    # The product-grain default level is materialised at 0, not deleted.
    null_levels = (
        db_session.query(StockLevel)
        .filter(StockLevel.product_id == product.id, StockLevel.variant_id.is_(None))
        .all()
    )
    assert [lvl.quantity for lvl in null_levels] == [0]


# ---------------------------------------------------------------------------
# Matrix reconcile — update / delete diff
# ---------------------------------------------------------------------------


def test_apply_matrix_updates_existing_and_deletes_removed(db_session, product_factory):
    product = product_factory(stock=0)
    crud_product_variant.apply_matrix(db_session, product=product, matrix=_color_size_matrix())
    db_session.commit()
    db_session.refresh(product)
    red, blue = crud_product_variant.get_for_product(db_session, product_id=product.id)

    # Keep Red (matched by id, new price), drop Blue, add Green.
    second = _matrix(
        options=[
            {
                "name": "Color",
                "position": 0,
                "values": [
                    {"value": "Red", "position": 0},
                    {"value": "Green", "position": 1},
                ],
            },
            {"name": "Size", "position": 1, "values": [{"value": "XL", "position": 0}]},
        ],
        variants=[
            {
                "id": red.id,
                "option_values": ["Red", "XL"],
                "sku": "VAR-RED-XL",
                "price_usd": 12.5,
            },
            {"option_values": ["Green", "XL"], "sku": "VAR-GREEN-XL", "initial_stock": 3},
        ],
    )
    result = crud_product_variant.apply_matrix(db_session, product=product, matrix=second)
    db_session.commit()
    db_session.refresh(product)

    assert result.updated_ids == [red.id]
    assert result.deleted_ids == [blue.id]
    assert len(result.created_ids) == 1

    variants = crud_product_variant.get_for_product(db_session, product_id=product.id)
    assert {variant.title for variant in variants} == {"Red / XL", "Green / XL"}
    red_after = next(variant for variant in variants if variant.id == red.id)
    assert red_after.price_usd == 12.5
    assert red_after.stock == 4  # initial_stock is ignored for existing variants
    # Blue's stock (6) was zeroed through the ledger before deletion.
    assert product.stock == 4 + 3
    removal = (
        db_session.query(InventoryMovement)
        .filter_by(product_id=product.id, note="variant removed")
        .all()
    )
    assert [(m.delta) for m in removal] == [-6]


def test_apply_matrix_matches_new_rows_by_signature(db_session, product_factory):
    """A payload row without an id whose combination already exists updates
    the existing variant instead of colliding."""
    product = product_factory(stock=0)
    crud_product_variant.apply_matrix(db_session, product=product, matrix=_color_size_matrix())
    db_session.commit()
    db_session.refresh(product)

    resent = _color_size_matrix()  # ids absent, same combinations
    result = crud_product_variant.apply_matrix(db_session, product=product, matrix=resent)
    db_session.commit()

    assert result.created_ids == []
    assert len(result.updated_ids) == 2
    assert result.deleted_ids == []


def test_apply_matrix_value_rename_rebuilds_titles(db_session, product_factory):
    product = product_factory(stock=0)
    crud_product_variant.apply_matrix(db_session, product=product, matrix=_color_size_matrix())
    db_session.commit()
    db_session.refresh(product)
    red, _blue = crud_product_variant.get_for_product(db_session, product_id=product.id)

    renamed = _matrix(
        options=[
            {
                "name": "Color",
                "position": 0,
                "values": [
                    {"value": "Crimson", "position": 0},
                    {"value": "Blue", "position": 1},
                ],
            },
            {"name": "Size", "position": 1, "values": [{"value": "XL", "position": 0}]},
        ],
        variants=[
            {"id": red.id, "option_values": ["Crimson", "XL"], "sku": "VAR-RED-XL"},
        ],
    )
    crud_product_variant.apply_matrix(db_session, product=product, matrix=renamed)
    db_session.commit()
    db_session.refresh(red)

    assert red.title == "Crimson / XL"
    # The signature follows the new value ids.
    crimson_ids = sorted(value.id for value in red.option_values)
    assert red.option_signature == "-".join(str(i) for i in crimson_ids)


def test_apply_matrix_empty_reverts_to_single_sku(db_session, product_factory):
    product = product_factory(stock=0)
    crud_product_variant.apply_matrix(db_session, product=product, matrix=_color_size_matrix())
    db_session.commit()
    db_session.refresh(product)
    assert product.stock == 10

    crud_product_variant.apply_matrix(
        db_session, product=product, matrix=_matrix(options=[], variants=[])
    )
    db_session.commit()
    db_session.refresh(product)

    assert product.options == []
    assert product.variants == []
    assert product.stock == 0  # variant stock zeroed; restock via inventory endpoints
    assert db_session.query(ProductVariant).filter_by(product_id=product.id).count() == 0
    assert db_session.query(ProductOptionValue).count() == 0


# ---------------------------------------------------------------------------
# Caps + structural rejections
# ---------------------------------------------------------------------------


def test_schema_rejects_a_fourth_option():
    with pytest.raises(ValidationError):
        _matrix(
            options=[
                {"name": f"Option {i}", "position": i, "values": [{"value": "V"}]} for i in range(4)
            ],
            variants=[],
        )


def test_crud_rejects_a_fourth_option_when_schema_is_bypassed(db_session, product_factory):
    product = product_factory(stock=0)
    matrix = ProductOptionsMatrixUpdate.model_construct(
        options=[
            ProductOptionInput.model_construct(
                name=f"Option {i}",
                name_pl=None,
                position=i,
                values=[ProductOptionValueInput(value="V")],
            )
            for i in range(4)
        ],
        variants=[],
    )
    with pytest.raises(VariantMatrixError, match="at most 3 options"):
        crud_product_variant.apply_matrix(db_session, product=product, matrix=matrix)


def test_schema_rejects_101_variants():
    option = {
        "name": "Number",
        "position": 0,
        "values": [{"value": str(i), "position": i} for i in range(101)],
    }
    with pytest.raises(ValidationError):
        _matrix(
            options=[option],
            variants=[{"option_values": [str(i)]} for i in range(101)],
        )


def test_crud_rejects_101_variants_when_schema_is_bypassed(db_session, product_factory):
    product = product_factory(stock=0)
    matrix = ProductOptionsMatrixUpdate.model_construct(
        options=[
            ProductOptionInput.model_construct(
                name="Number",
                name_pl=None,
                position=0,
                values=[ProductOptionValueInput(value=str(i)) for i in range(101)],
            )
        ],
        variants=[
            ProductVariantInput.model_construct(
                id=None,
                option_values=[str(i)],
                sku=None,
                price_usd=None,
                price_pln=None,
                price_eur=None,
                image_id=None,
                is_active=True,
                position=i,
                initial_stock=None,
            )
            for i in range(101)
        ],
    )
    with pytest.raises(VariantMatrixError, match="at most 100 variants"):
        crud_product_variant.apply_matrix(db_session, product=product, matrix=matrix)


def test_apply_matrix_rejects_duplicate_combination(db_session, product_factory):
    product = product_factory(stock=0)
    matrix = _matrix(
        options=[
            {
                "name": "Color",
                "position": 0,
                "values": [{"value": "Red", "position": 0}],
            }
        ],
        variants=[
            {"option_values": ["Red"]},
            {"option_values": ["Red"]},
        ],
    )
    with pytest.raises(VariantMatrixError, match="Duplicate variant combination"):
        crud_product_variant.apply_matrix(db_session, product=product, matrix=matrix)


def test_apply_matrix_rejects_unknown_value(db_session, product_factory):
    product = product_factory(stock=0)
    matrix = _matrix(
        options=[{"name": "Color", "position": 0, "values": [{"value": "Red", "position": 0}]}],
        variants=[{"option_values": ["Chartreuse"]}],
    )
    with pytest.raises(VariantMatrixError, match="Unknown value"):
        crud_product_variant.apply_matrix(db_session, product=product, matrix=matrix)


def test_apply_matrix_rejects_foreign_variant_id(db_session, product_factory):
    product_a = product_factory(stock=0)
    product_b = product_factory(stock=0)
    crud_product_variant.apply_matrix(db_session, product=product_a, matrix=_color_size_matrix())
    db_session.commit()
    db_session.refresh(product_a)
    foreign = crud_product_variant.get_for_product(db_session, product_id=product_a.id)[0]

    matrix = _matrix(
        options=[{"name": "Color", "position": 0, "values": [{"value": "Red", "position": 0}]}],
        variants=[{"id": foreign.id, "option_values": ["Red"]}],
    )
    with pytest.raises(VariantMatrixError, match="does not belong"):
        crud_product_variant.apply_matrix(db_session, product=product_b, matrix=matrix)


def test_apply_matrix_rejects_taken_sku(db_session, product_factory):
    product_a = product_factory(stock=0)
    product_b = product_factory(stock=0)
    crud_product_variant.apply_matrix(db_session, product=product_a, matrix=_color_size_matrix())
    db_session.commit()

    matrix = _matrix(
        options=[{"name": "Color", "position": 0, "values": [{"value": "Red", "position": 0}]}],
        variants=[{"option_values": ["Red"], "sku": "VAR-RED-XL"}],  # taken by product_a
    )
    with pytest.raises(VariantSkuConflictError, match="already in use"):
        crud_product_variant.apply_matrix(db_session, product=product_b, matrix=matrix)


# ---------------------------------------------------------------------------
# delete_variant
# ---------------------------------------------------------------------------


def test_delete_variant_zeroes_stock_through_the_ledger(db_session, product_factory):
    product = product_factory(stock=0)
    crud_product_variant.apply_matrix(db_session, product=product, matrix=_color_size_matrix())
    db_session.commit()
    db_session.refresh(product)
    _red, blue = crud_product_variant.get_for_product(db_session, product_id=product.id)

    crud_product_variant.delete_variant(db_session, product=product, variant=blue)
    db_session.commit()
    db_session.refresh(product)

    assert product.stock == 4  # only Red's seed remains
    deletion = (
        db_session.query(InventoryMovement)
        .filter_by(product_id=product.id, note="variant deleted")
        .all()
    )
    # The ledger row survives the variant deletion with variant_id SET NULL.
    assert [(m.delta, m.variant_id) for m in deletion] == [(-6, None)]
    assert crud_product_variant.get(db_session, product_id=product.id, variant_id=blue.id) is None


# ---------------------------------------------------------------------------
# get_price inheritance
# ---------------------------------------------------------------------------


def test_variant_get_price_inheritance_chain(db_session, product_factory):
    product = product_factory(stock=0, price_usd=10.0, price_pln=40.0, price_eur=9.0)
    matrix = _matrix(
        options=[{"name": "Color", "position": 0, "values": [{"value": "Red", "position": 0}]}],
        variants=[{"option_values": ["Red"], "price_usd": 15.0}],
    )
    crud_product_variant.apply_matrix(db_session, product=product, matrix=matrix)
    db_session.commit()
    db_session.refresh(product)
    variant = product.variants[0]

    assert variant.get_price("USD") == 15.0  # own override
    assert variant.get_price("PLN") == 40.0  # NULL -> inherits the product
    assert variant.get_price("EUR") == 9.0
    assert variant.effective_price_usd == 15.0
    assert variant.effective_price_pln == 40.0
