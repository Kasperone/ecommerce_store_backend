"""Seed a small demo catalog so a freshly bootstrapped store is browsable.

    python -m app.scripts.seed_demo

Creates (idempotent — get-or-create on slug/code, existing rows are skipped
entirely, never updated):

- 3 categories: "Demo Apparel" (parent) with "Demo T-Shirts" and
  "Demo Mugs & Accessories" children — all with Polish copy.
- 6 products across those categories, each with prices in all three
  currencies (USD/PLN/EUR), full PL translations (``name_pl`` /
  ``description_pl`` — the ``audit_translations --all`` gate stays green),
  stock, and varied tax categories (one ``REDUCED_8`` item to demo the
  per-rate faktura decomposition). One product is ``is_featured``.
- One product ("Demo Classic T-Shirt") gets a 2-option variant matrix
  (Size S/M/L x Color Black/White = 6 variants) with per-variant SKU,
  per-variant prices in all three currencies, and per-variant stock —
  written through ``crud_product_variant.apply_matrix`` so the inventory
  ledger and denormalized stock stay consistent.
- 1 manual collection ``demo-bestsellers`` containing 3 of the products.
- 1 coupon ``DEMO10`` — 10% off, active, no expiry, no minimum.

Every object carries a ``demo-`` slug prefix (coupon code ``DEMO10``,
SKUs ``DEMO-*``) so demo data is identifiable and easy to bulk-delete
before go-live. No product images are seeded (the R2 pipeline is out of
scope — the storefront renders products without images via its placeholder).

OMNIBUS: new products/variants get their prices recorded into the price
history (same as the admin create routes) so the 30-day-low query has an
honest ground state for demo products too.

Exits 0 on success; non-zero on any unexpected error so scripted
provisioning fails loudly.
"""

from __future__ import annotations

import logging
import sys
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.crud.collection import collection as crud_collection
from app.crud.price_history import price_history as crud_price_history
from app.crud.product_variant import product_variant as crud_product_variant
from app.models.category import Category
from app.models.collection import Collection
from app.models.coupon import Coupon, CouponDiscountType
from app.models.price_history import SUPPORTED_CURRENCIES
from app.models.product import Product
from app.models.tax import TaxCategory
from app.schemas.product_variant import (
    ProductOptionInput,
    ProductOptionsMatrixUpdate,
    ProductOptionValueInput,
    ProductVariantInput,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# Category catalog — one parent, two children (parent_slug wires the tree)
# ---------------------------------------------------------------------------

CATEGORIES: list[dict[str, Any]] = [
    {
        "slug": "demo-apparel",
        "name": "Demo Apparel",
        "name_pl": "Odzież demo",
        "description": "Demo parent category for apparel and accessories.",
        "description_pl": "Przykładowa kategoria nadrzędna z odzieżą i akcesoriami.",
        "parent_slug": None,
        "sort_order": 10,
    },
    {
        "slug": "demo-t-shirts",
        "name": "Demo T-Shirts",
        "name_pl": "Koszulki demo",
        "description": "Demo t-shirts in a range of sizes and colors.",
        "description_pl": "Przykładowe koszulki w różnych rozmiarach i kolorach.",
        "parent_slug": "demo-apparel",
        "sort_order": 10,
    },
    {
        "slug": "demo-mugs-accessories",
        "name": "Demo Mugs & Accessories",
        "name_pl": "Kubki i akcesoria demo",
        "description": "Demo mugs, bags and everyday accessories.",
        "description_pl": "Przykładowe kubki, torby i akcesoria na co dzień.",
        "parent_slug": "demo-apparel",
        "sort_order": 20,
    },
]


# ---------------------------------------------------------------------------
# Product catalog — 6 products, all three currencies, full PL copy
# ---------------------------------------------------------------------------

PRODUCTS: list[dict[str, Any]] = [
    {
        # The variant product — see VARIANT_MATRIX below. Product-grain stock
        # starts at 0; apply_matrix seeds each variant's initial stock through
        # the inventory ledger (6 x 10 = 60 total).
        "slug": "demo-classic-tee",
        "name": "Demo Classic T-Shirt",
        "name_pl": "Klasyczna koszulka demo",
        "description": "A soft cotton t-shirt available in three sizes and two colors.",
        "description_pl": "Miękka bawełniana koszulka w trzech rozmiarach i dwóch kolorach.",
        "sku": "DEMO-TEE-CLASSIC",
        "category_slug": "demo-t-shirts",
        "price_usd": Decimal("19.99"),
        "price_pln": Decimal("79.99"),
        "price_eur": Decimal("18.99"),
        "tax_category": "STANDARD",
        "stock": 0,
        "is_featured": True,
        "has_variants": True,
    },
    {
        "slug": "demo-graphic-tee",
        "name": "Demo Graphic T-Shirt",
        "name_pl": "Koszulka demo z nadrukiem",
        "description": "A statement t-shirt with a bold front print.",
        "description_pl": "Wyrazista koszulka z dużym nadrukiem z przodu.",
        "sku": "DEMO-TEE-GRAPHIC",
        "category_slug": "demo-t-shirts",
        "price_usd": Decimal("24.99"),
        "price_pln": Decimal("99.99"),
        "price_eur": Decimal("22.99"),
        "tax_category": "STANDARD",
        "stock": 40,
    },
    {
        "slug": "demo-longsleeve-tee",
        "name": "Demo Long Sleeve T-Shirt",
        "name_pl": "Koszulka demo z długim rękawem",
        "description": "A long sleeve t-shirt for cooler days.",
        "description_pl": "Koszulka z długim rękawem na chłodniejsze dni.",
        "sku": "DEMO-TEE-LONGSLEEVE",
        "category_slug": "demo-t-shirts",
        "price_usd": Decimal("29.99"),
        "price_pln": Decimal("119.99"),
        "price_eur": Decimal("27.99"),
        "tax_category": "STANDARD",
        "stock": 30,
    },
    {
        # Reduced VAT rate to demo the per-rate faktura decomposition.
        "slug": "demo-ceramic-mug",
        "name": "Demo Ceramic Mug",
        "name_pl": "Kubek ceramiczny demo",
        "description": "A 350 ml ceramic mug, dishwasher and microwave safe.",
        "description_pl": "Kubek ceramiczny 350 ml, można myć w zmywarce i używać w mikrofalówce.",
        "sku": "DEMO-MUG-CERAMIC",
        "category_slug": "demo-mugs-accessories",
        "price_usd": Decimal("12.99"),
        "price_pln": Decimal("49.99"),
        "price_eur": Decimal("11.99"),
        "tax_category": "REDUCED_8",
        "stock": 80,
    },
    {
        "slug": "demo-travel-mug",
        "name": "Demo Travel Mug",
        "name_pl": "Kubek termiczny demo",
        "description": "A 450 ml insulated travel mug with a leak-proof lid.",
        "description_pl": "Kubek termiczny 450 ml ze szczelną pokrywką.",
        "sku": "DEMO-MUG-TRAVEL",
        "category_slug": "demo-mugs-accessories",
        "price_usd": Decimal("22.99"),
        "price_pln": Decimal("89.99"),
        "price_eur": Decimal("20.99"),
        "tax_category": "STANDARD",
        "stock": 60,
    },
    {
        "slug": "demo-tote-bag",
        "name": "Demo Tote Bag",
        "name_pl": "Torba demo na zakupy",
        "description": "A sturdy canvas tote bag for everyday errands.",
        "description_pl": "Solidna płócienna torba na codzienne zakupy.",
        "sku": "DEMO-BAG-TOTE",
        "category_slug": "demo-mugs-accessories",
        "price_usd": Decimal("14.99"),
        "price_pln": Decimal("59.99"),
        "price_eur": Decimal("13.99"),
        "tax_category": "STANDARD",
        "stock": 100,
    },
]


# ---------------------------------------------------------------------------
# Variant matrix for demo-classic-tee — Size x Color = 6 variants
# ---------------------------------------------------------------------------

VARIANT_PRODUCT_SLUG = "demo-classic-tee"

_SIZES = ["S", "M", "L"]
_COLORS = [("Black", "Czarny"), ("White", "Biały")]

# Per-variant price in all three currencies — L costs a little more, so the
# demo shows a per-variant price override actually differing across variants.
_VARIANT_PRICES: dict[str, dict[str, float]] = {
    "S": {"usd": 19.99, "pln": 79.99, "eur": 18.99},
    "M": {"usd": 19.99, "pln": 79.99, "eur": 18.99},
    "L": {"usd": 21.99, "pln": 89.99, "eur": 20.99},
}
_VARIANT_STOCK = 10


def _variant_matrix() -> ProductOptionsMatrixUpdate:
    """The desired option/variant state for the demo variant product."""
    options = [
        ProductOptionInput(
            name="Size",
            name_pl="Rozmiar",
            position=0,
            values=[
                ProductOptionValueInput(value=size, position=index)
                for index, size in enumerate(_SIZES)
            ],
        ),
        ProductOptionInput(
            name="Color",
            name_pl="Kolor",
            position=1,
            values=[
                ProductOptionValueInput(value=color, value_pl=color_pl, position=index)
                for index, (color, color_pl) in enumerate(_COLORS)
            ],
        ),
    ]
    combos = [(size, color) for size in _SIZES for color, _ in _COLORS]
    variants = [
        ProductVariantInput(
            option_values=[size, color],
            sku=f"DEMO-TEE-CLASSIC-{size}-{color[:3].upper()}",
            price_usd=_VARIANT_PRICES[size]["usd"],
            price_pln=_VARIANT_PRICES[size]["pln"],
            price_eur=_VARIANT_PRICES[size]["eur"],
            initial_stock=_VARIANT_STOCK,
            position=position,
        )
        for position, (size, color) in enumerate(combos)
    ]
    return ProductOptionsMatrixUpdate(options=options, variants=variants)


# ---------------------------------------------------------------------------
# Collection + coupon
# ---------------------------------------------------------------------------

COLLECTION: dict[str, Any] = {
    "slug": "demo-bestsellers",
    "name": "Demo Bestsellers",
    "name_pl": "Bestsellery demo",
    "description": "A hand-picked demo selection of our most popular products.",
    "description_pl": "Ręcznie wybrane, najpopularniejsze produkty demo.",
    "product_slugs": ["demo-classic-tee", "demo-ceramic-mug", "demo-tote-bag"],
    "sort_order": 10,
}

COUPON: dict[str, Any] = {
    "code": "DEMO10",
    "description": "Demo coupon — 10% off any order.",
    "discount_type": CouponDiscountType.PERCENT.value,
    "value": Decimal("10.00"),
}


# ---------------------------------------------------------------------------
# Seeders
# ---------------------------------------------------------------------------


def _get_or_create_category(
    db: Session, *, spec: dict[str, Any], parents: dict[str, Category]
) -> Category:
    """Idempotent category upsert by slug — an existing row is skipped as-is."""
    category = db.query(Category).filter(Category.slug == spec["slug"]).first()
    if category is not None:
        logger.info("category exists: %s (skip)", spec["slug"])
        return category
    parent = parents.get(spec["parent_slug"]) if spec["parent_slug"] else None
    category = Category(
        name=spec["name"],
        slug=spec["slug"],
        description=spec["description"],
        name_pl=spec["name_pl"],
        description_pl=spec["description_pl"],
        parent_id=parent.id if parent is not None else None,
        sort_order=spec["sort_order"],
        is_active=True,
    )
    db.add(category)
    db.flush()
    logger.info("category created: %s", spec["slug"])
    return category


def _record_product_prices(db: Session, product: Product) -> None:
    """OMNIBUS ground state for a new product (mirrors the admin create route)."""
    for currency in SUPPORTED_CURRENCIES:
        crud_price_history.record_if_changed(
            db,
            product=product,
            currency=currency,
            new_price=getattr(product, f"price_{currency.lower()}"),
        )


def _get_or_create_product(
    db: Session, *, spec: dict[str, Any], categories: dict[str, Category]
) -> tuple[Product, bool]:
    """Idempotent product upsert by slug. Returns ``(product, created)``."""
    product = db.query(Product).filter(Product.slug == spec["slug"]).first()
    if product is not None:
        logger.info("product exists: %s (skip)", spec["slug"])
        return product, False
    product = Product(
        name=spec["name"],
        slug=spec["slug"],
        description=spec["description"],
        name_pl=spec["name_pl"],
        description_pl=spec["description_pl"],
        sku=spec["sku"],
        category_id=categories[spec["category_slug"]].id,
        price_usd=spec["price_usd"],
        price_pln=spec["price_pln"],
        price_eur=spec["price_eur"],
        # Categories are seeded by the tax-categories migration; resolve by code.
        tax_category_id=db.query(TaxCategory).filter_by(code=spec["tax_category"]).one().id,
        stock=spec["stock"],
        is_active=True,
        is_featured=spec.get("is_featured", False),
    )
    db.add(product)
    db.flush()
    _record_product_prices(db, product)
    logger.info("product created: %s (tax=%s)", spec["slug"], spec["tax_category"])
    return product, True


def _seed_variants(db: Session, *, product: Product) -> None:
    """Apply the Size x Color matrix to a **newly created** variant product.

    Goes through ``apply_matrix`` so per-variant stock is seeded via the
    inventory ledger and ``product.stock`` lands at the variant sum. Each
    variant's prices are recorded into the OMNIBUS history (mirrors the
    admin matrix route).
    """
    result = crud_product_variant.apply_matrix(db, product=product, matrix=_variant_matrix())
    for variant in result.variants:
        for currency in SUPPORTED_CURRENCIES:
            crud_price_history.record_if_changed(
                db,
                product=product,
                currency=currency,
                new_price=getattr(variant, f"price_{currency.lower()}"),
                variant_id=variant.id,
            )
    logger.info(
        "variants created: %s (%d variants, %d units each)",
        product.slug,
        len(result.created_ids),
        _VARIANT_STOCK,
    )


def _get_or_create_collection(db: Session, *, spec: dict[str, Any]) -> Collection:
    """Idempotent collection upsert by slug — an existing row keeps its membership."""
    collection = db.query(Collection).filter(Collection.slug == spec["slug"]).first()
    if collection is not None:
        logger.info("collection exists: %s (skip)", spec["slug"])
        return collection
    collection = Collection(
        name=spec["name"],
        slug=spec["slug"],
        description=spec["description"],
        name_pl=spec["name_pl"],
        description_pl=spec["description_pl"],
        sort_order=spec["sort_order"],
        is_active=True,
        type="manual",
    )
    db.add(collection)
    db.flush()
    member_ids = [
        product.id
        for slug in spec["product_slugs"]
        if (product := db.query(Product).filter(Product.slug == slug).first()) is not None
    ]
    crud_collection.set_products(db, collection_id=collection.id, product_ids=member_ids)
    logger.info("collection created: %s with %d products", spec["slug"], len(member_ids))
    return collection


def _get_or_create_coupon(db: Session, *, spec: dict[str, Any]) -> Coupon:
    """Idempotent coupon upsert by code — an existing row is skipped as-is."""
    coupon = db.query(Coupon).filter(Coupon.code == spec["code"]).first()
    if coupon is not None:
        logger.info("coupon exists: %s (skip)", spec["code"])
        return coupon
    coupon = Coupon(
        code=spec["code"],
        description=spec["description"],
        discount_type=spec["discount_type"],
        value=spec["value"],
        currency=None,  # percent coupons are currency-agnostic
        min_order_amount=None,  # permissive — applies to any order
        starts_at=None,
        ends_at=None,  # no expiry
        is_active=True,
    )
    db.add(coupon)
    db.flush()
    logger.info("coupon created: %s (%s%% off)", spec["code"], spec["value"])
    return coupon


def seed_demo(db: Session) -> None:
    """Idempotent end-to-end demo-catalog seed. Commits once at the end."""
    categories: dict[str, Category] = {}
    for spec in CATEGORIES:
        categories[spec["slug"]] = _get_or_create_category(db, spec=spec, parents=categories)

    for spec in PRODUCTS:
        product, created = _get_or_create_product(db, spec=spec, categories=categories)
        # Variants only on first creation — an existing demo product (and
        # whatever an admin did to its matrix since) is left untouched.
        if created and spec.get("has_variants"):
            _seed_variants(db, product=product)

    _get_or_create_collection(db, spec=COLLECTION)
    _get_or_create_coupon(db, spec=COUPON)

    db.commit()
    logger.info("demo catalog seed complete")


def main() -> int:
    db = SessionLocal()
    try:
        seed_demo(db)
    except Exception:
        logger.exception("demo catalog seed failed")
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
