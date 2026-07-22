"""Factory-boy factory for Product.

Session binding follows the same pattern as ``UserFactory`` — populated
dynamically by a per-test fixture in ``tests/conftest.py``.

``product.vat_rate`` no longer exists (products are classified by tax category),
but a legacy ``vat_rate=N`` kwarg stays supported as a **test convenience**: it
resolves to the category whose in-force PL rate is ``N`` (the tax-categories
migration seeds those rows), so existing tests that say "a product taxed at 8%"
keep expressing exactly that and still assert on the resolved order-line rate.
"""

from __future__ import annotations

import factory
from factory.alchemy import SQLAlchemyModelFactory
from sqlalchemy import select

from app.models.product import Product
from app.models.tax import TaxCategory, TaxRate
from app.services.tax import get_default_category
from tests.factories.category import CategoryFactory


def _tax_category_id_for_rate(rate: int) -> int:
    """Map a legacy Polish ``vat_rate`` to the seeded category with that rate.

    The categories/rates are committed by the migration, so this reads the same
    session the factory is bound to. Falls back to the store default when no
    category carries the rate (mirrors the retired ``category_for_product``)."""
    session = ProductFactory._meta.sqlalchemy_session  # pyright: ignore[reportAttributeAccessIssue] -- bound by the product_factory fixture
    assert session is not None, "ProductFactory session not bound (use the product_factory fixture)"
    category = session.scalars(
        select(TaxCategory)
        .join(TaxRate, TaxRate.tax_category_id == TaxCategory.id)
        .where(
            TaxRate.country_code == "PL",
            TaxRate.rate_pct == rate,
            TaxRate.valid_to.is_(None),
        )
        .order_by(TaxCategory.id)
    ).first()
    return (category or get_default_category(session)).id


class ProductFactory(SQLAlchemyModelFactory):
    class Meta:  # pyright: ignore[reportIncompatibleVariableOverride] -- factory-boy idiom; Meta is the documented config hook
        model = Product
        sqlalchemy_session = None  # populated by the product_factory fixture
        sqlalchemy_session_persistence = "commit"

    class Params:
        # Transient (not a Product column): the legacy convenience described above.
        vat_rate = 23

    name = factory.Sequence(lambda n: f"Product {n}")
    slug = factory.Sequence(lambda n: f"product-{n}")
    description = factory.Faker("paragraph")
    sku = factory.Sequence(lambda n: f"SKU-{n:05d}")
    price_usd = 19.99
    price_pln = 79.99
    price_eur = 17.99
    stock = 10
    is_active = True
    is_featured = False
    category = factory.SubFactory(CategoryFactory)
    tax_category_id = factory.LazyAttribute(lambda o: _tax_category_id_for_rate(o.vat_rate))
