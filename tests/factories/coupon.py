"""Factory-boy factory for Coupon.

Session binding follows the same pattern as ``UserFactory`` — populated
dynamically by a per-test fixture in ``tests/conftest.py``. Defaults to a valid
``percent`` coupon (no currency); pass ``discount_type="fixed", currency="USD"``
for a fixed-amount one.
"""

from __future__ import annotations

import factory
from factory.alchemy import SQLAlchemyModelFactory

from app.models.coupon import Coupon


class CouponFactory(SQLAlchemyModelFactory):
    class Meta:  # pyright: ignore[reportIncompatibleVariableOverride] -- factory-boy idiom; Meta is the documented config hook
        model = Coupon
        sqlalchemy_session = None  # populated by the coupon_factory fixture
        sqlalchemy_session_persistence = "commit"

    code = factory.Sequence(lambda n: f"SAVE{n:04d}")
    description = factory.Faker("sentence")
    discount_type = "percent"
    value = 10.0
    currency = None
    min_order_amount = None
    max_uses = None
    per_user_limit = None
    used_count = 0
    starts_at = None
    ends_at = None
    is_active = True
