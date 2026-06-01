"""Banner model — admin-managed homepage hero / promo-strip content.

This is **management metadata only** (same posture as coupons): a banner records
the copy + image + optional link + an optional display window for a slot on the
storefront. **Rendering banners on the storefront is deliberately NOT modelled
here** — that is a separate, deferred storefront concern. The admin CRUD only
manages the rows.

``placement`` follows the ``RefundReason`` / ``CouponDiscountType`` convention: a
``str``-enum stored as the lowercase value in a plain ``String(16)`` column (not a
PG enum), guarded by a CHECK constraint. Adding a placement later is a code-only
change with no migration.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class BannerPlacement(str, enum.Enum):
    """Where a banner renders on the storefront.

    Stored as the lowercase value in a ``String(16)`` column (mirrors
    ``RefundReason``). ``HOME_HERO`` is the large homepage hero slot;
    ``PROMO_STRIP`` is a slimmer promotional strip.
    """

    HOME_HERO = "home_hero"
    PROMO_STRIP = "promo_strip"


# Mirror of ``BannerPlacement.values``, used by the DB CHECK constraint and
# referenced by ``app/schemas/banner.py`` so the route layer, DB layer, and
# OpenAPI schema can never drift apart. Mirrors ``REFUND_REASON_VALUES``.
BANNER_PLACEMENT_VALUES: tuple[str, ...] = tuple(p.value for p in BannerPlacement)


class Banner(BaseModel):
    """An admin-managed storefront banner.

    ``subtitle`` and ``link_url`` are optional. ``placement`` is the storefront
    slot (CHECK-guarded). ``sort_order`` orders banners within a slot (lower
    first). ``starts_at`` / ``ends_at`` are an optional display window (both naive
    UTC, like every datetime column in this app); the schema enforces
    ``ends_at`` > ``starts_at`` when both are set. ``is_active`` lets an admin
    park a banner without deleting it.
    """

    __tablename__ = "banners"
    __table_args__ = (
        CheckConstraint(
            "placement IN ('" + "', '".join(BANNER_PLACEMENT_VALUES) + "')",
            name="ck_banners_placement_enum",
        ),
    )

    title: Mapped[str] = mapped_column(String(255))
    subtitle: Mapped[str | None] = mapped_column(String(500))
    image_url: Mapped[str] = mapped_column(String(2048))
    link_url: Mapped[str | None] = mapped_column(String(2048))
    placement: Mapped[str] = mapped_column(String(16))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    def __repr__(self) -> str:
        return f"<Banner {self.id} {self.placement!r} {self.title!r}>"
