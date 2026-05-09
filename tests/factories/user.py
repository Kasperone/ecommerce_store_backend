"""Factory-boy factories for User.

The session is bound dynamically by the ``user_factory`` fixture in
``tests/conftest.py`` so every test gets the per-test SQLAlchemy session.
"""

from __future__ import annotations

import factory
from factory.alchemy import SQLAlchemyModelFactory

from app.core.security import get_password_hash
from app.models.user import User, UserRole


# Default password used by the factory when no override is given. Tests can
# read this constant to authenticate as the created user.
DEFAULT_PASSWORD = "TestPass123!"


class UserFactory(SQLAlchemyModelFactory):
    class Meta:
        model = User
        sqlalchemy_session = None  # populated by the user_factory fixture
        sqlalchemy_session_persistence = "commit"

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    hashed_password = factory.LazyFunction(lambda: get_password_hash(DEFAULT_PASSWORD))
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    is_active = True
    is_verified = True
    role = UserRole.CUSTOMER


class UnverifiedUserFactory(UserFactory):
    is_verified = False


class AdminUserFactory(UserFactory):
    role = UserRole.ADMIN
