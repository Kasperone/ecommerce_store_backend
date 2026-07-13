"""Tests for the demo-catalog seeder (app/scripts/seed_demo.py).

DB-backed coverage: expected object counts, idempotency (a second run adds
nothing and updates nothing), full PL copy on every demo object (asserted via
the translation registry — the same completeness logic the pre-deploy
``audit_translations`` gate uses), and the variant matrix / VAT / coupon shape.
"""

from __future__ import annotations

import pytest

from app.models.category import Category
from app.models.collection import Collection
from app.models.coupon import Coupon
from app.models.product import Product
from app.models.product_variant import ProductOption, ProductVariant
from app.scripts.audit_translations import run_audit
from app.scripts.seed_demo import CATEGORIES, PRODUCTS, VARIANT_PRODUCT_SLUG, seed_demo

pytestmark = pytest.mark.integration

DEMO_PREFIX = "demo-"


def _demo_products(db) -> list[Product]:
    return db.query(Product).filter(Product.slug.like(f"{DEMO_PREFIX}%")).all()


def _demo_categories(db) -> list[Category]:
    return db.query(Category).filter(Category.slug.like(f"{DEMO_PREFIX}%")).all()


def test_seed_creates_expected_objects(db_session):
    seed_demo(db_session)

    categories = _demo_categories(db_session)
    products = _demo_products(db_session)
    assert len(categories) == len(CATEGORIES) == 3
    assert len(products) == len(PRODUCTS) == 6

    # Hierarchy: one parent, two children under it.
    parent = db_session.query(Category).filter(Category.slug == "demo-apparel").one()
    assert parent.parent_id is None
    children = [c for c in categories if c.parent_id == parent.id]
    assert len(children) == 2

    # Every product has all three currency prices and a demo- category.
    for product in products:
        assert product.price_usd is not None
        assert product.price_pln is not None
        assert product.price_eur is not None
        assert product.is_active is True

    # VAT variety: exactly one reduced-rate (8%) item, the rest standard 23%.
    assert sorted(p.vat_rate for p in products) == [8, 23, 23, 23, 23, 23]
    # Exactly one featured product.
    assert sum(1 for p in products if p.is_featured) == 1

    # Collection: demo-bestsellers with 3 members and PL copy.
    collection = db_session.query(Collection).filter(Collection.slug == "demo-bestsellers").one()
    assert collection.type == "manual"
    assert len(collection.products) == 3

    # Coupon: DEMO10, 10% percent, active, no expiry, no minimum.
    coupon = db_session.query(Coupon).filter(Coupon.code == "DEMO10").one()
    assert coupon.discount_type == "percent"
    assert coupon.value == 10
    assert coupon.is_active is True
    assert coupon.ends_at is None
    assert coupon.min_order_amount is None


def test_variant_product_matrix(db_session):
    seed_demo(db_session)

    product = db_session.query(Product).filter(Product.slug == VARIANT_PRODUCT_SLUG).one()
    options = db_session.query(ProductOption).filter(ProductOption.product_id == product.id).all()
    variants = (
        db_session.query(ProductVariant).filter(ProductVariant.product_id == product.id).all()
    )
    assert [o.name for o in sorted(options, key=lambda o: o.position)] == ["Size", "Color"]
    assert len(variants) == 6  # 3 sizes x 2 colors

    for variant in variants:
        assert variant.sku and variant.sku.startswith("DEMO-")
        assert variant.price_usd is not None
        assert variant.price_pln is not None
        assert variant.price_eur is not None
        assert variant.stock == 10  # seeded through the inventory ledger

    # Denormalized product stock is the variant sum.
    db_session.refresh(product)
    assert product.stock == 60


def test_second_run_is_idempotent(db_session):
    seed_demo(db_session)
    products_before = {p.slug: p.updated_at for p in _demo_products(db_session)}

    seed_demo(db_session)

    assert len(_demo_categories(db_session)) == 3
    assert len(_demo_products(db_session)) == 6
    assert db_session.query(Coupon).filter(Coupon.code == "DEMO10").count() == 1
    assert db_session.query(Collection).filter(Collection.slug == "demo-bestsellers").count() == 1
    variant_product = db_session.query(Product).filter(Product.slug == VARIANT_PRODUCT_SLUG).one()
    assert (
        db_session.query(ProductVariant)
        .filter(ProductVariant.product_id == variant_product.id)
        .count()
        == 6
    )
    # No updates either — exists → skip, not upsert.
    assert {p.slug: p.updated_at for p in _demo_products(db_session)} == products_before


def test_demo_content_passes_translation_audit(db_session):
    """Every demo product/category/collection is PL-complete — the pre-deploy
    ``audit_translations --all`` gate must stay green after seeding."""
    seed_demo(db_session)

    report = run_audit(
        db_session, locale="pl", resource_types=["product", "category", "collection"]
    )
    assert report.ok, f"incomplete: {[(i.resource_type, i.label) for i in report.incomplete]}"
    assert report.scanned == 10  # 6 products + 3 categories + 1 collection

    # Belt-and-braces: name_pl is non-empty on every demo object directly.
    for category in _demo_categories(db_session):
        assert category.name_pl
    for product in _demo_products(db_session):
        assert product.name_pl
        assert product.description_pl
