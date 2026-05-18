"""Factory-boy factory for Category.

Session binding follows the same pattern as ``UserFactory`` — populated
dynamically by a per-test fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

import factory
from factory.alchemy import SQLAlchemyModelFactory

from app.models.category import Category


class CategoryFactory(SQLAlchemyModelFactory):
    class Meta:  # pyright: ignore[reportIncompatibleVariableOverride] -- factory-boy idiom; Meta is the documented config hook
        model = Category
        sqlalchemy_session = None  # populated by the category_factory fixture
        sqlalchemy_session_persistence = "commit"

    name = factory.Sequence(lambda n: f"Category {n}")
    slug = factory.Sequence(lambda n: f"category-{n}")
    description = factory.Faker("sentence")
    is_active = True
