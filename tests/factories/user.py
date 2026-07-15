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
    class Meta:  # pyright: ignore[reportIncompatibleVariableOverride] -- factory-boy idiom; Meta is the documented config hook
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
    # Mirror the A1 migration backfill: an ADMIN-role account is an owner
    # (implicitly all capabilities) unless a test overrides is_owner explicitly
    # to build a scoped staffer. Customers are never owners.
    is_owner = factory.LazyAttribute(lambda o: o.role == UserRole.ADMIN)
    capabilities = factory.LazyFunction(list)


class UnverifiedUserFactory(UserFactory):
    is_verified = False


class AdminUserFactory(UserFactory):
    role = UserRole.ADMIN
