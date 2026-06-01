"""Factory-boy factory for Banner.

Session binding follows the same pattern as ``CouponFactory`` — populated
dynamically by a per-test fixture. Defaults to an active ``home_hero`` banner;
pass ``placement="promo_strip"`` for a promo strip.
"""

from __future__ import annotations

import factory
from factory.alchemy import SQLAlchemyModelFactory

from app.models.banner import Banner


class BannerFactory(SQLAlchemyModelFactory):
    class Meta:  # pyright: ignore[reportIncompatibleVariableOverride] -- factory-boy idiom; Meta is the documented config hook
        model = Banner
        sqlalchemy_session = None  # populated by the banner_factory fixture
        sqlalchemy_session_persistence = "commit"

    title = factory.Sequence(lambda n: f"Banner {n}")
    subtitle = None
    image_url = factory.Sequence(lambda n: f"https://cdn.example.com/banner-{n}.jpg")
    link_url = None
    placement = "home_hero"
    sort_order = 0
    starts_at = None
    ends_at = None
    is_active = True
