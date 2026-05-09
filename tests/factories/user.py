"""Factory-boy factories for User.

The session is bound dynamically by the ``user_factory`` fixture in
``tests/conftest.py`` so every test gets the per-test SQLAlchemy session.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import factory
from factory.alchemy import SQLAlchemyModelFactory

from app.core.security import get_password_hash
from app.models.user import User, UserRole


# Default password used by the factory when no override is given. Tests can
# read this constant to authenticate as the created user.
DEFAULT_PASSWORD = "TestPass123!"


def _password_changed_default() -> datetime:
    """Backdate by a minute so JWTs minted right after creation pass the
    ``iat < password_changed_at`` access-token guard.

    The model default is ``_utcnow`` (microsecond precision) but JWT ``iat``
    is truncated to whole seconds — a token issued in the same second as
    user creation can have an ``iat`` strictly less than ``password_changed_at``,
    which makes the dependency reject it. In production this is impossible
    (verification → login takes seconds at minimum); in tests we need to
    sidestep the precision mismatch.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)


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
    password_changed_at = factory.LazyFunction(_password_changed_default)


class UnverifiedUserFactory(UserFactory):
    is_verified = False


class AdminUserFactory(UserFactory):
    role = UserRole.ADMIN
