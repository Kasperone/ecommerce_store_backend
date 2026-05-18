"""Factory-boy factory for Product.

Session binding follows the same pattern as ``UserFactory`` — populated
dynamically by a per-test fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

import factory
from factory.alchemy import SQLAlchemyModelFactory

from app.models.product import Product
from tests.factories.category import CategoryFactory


class ProductFactory(SQLAlchemyModelFactory):
    class Meta:  # pyright: ignore[reportIncompatibleVariableOverride] -- factory-boy idiom; Meta is the documented config hook
        model = Product
        sqlalchemy_session = None  # populated by the product_factory fixture
        sqlalchemy_session_persistence = "commit"

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
