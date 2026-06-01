"""Pydantic schemas for admin banner management.

``BannerPlacementLiteral`` is hardcoded (pyright needs literal arguments in type
positions) but cross-checked against the model's ``BANNER_PLACEMENT_VALUES`` at
import time, so the DB CHECK, the schema, and the route can't drift apart
silently. Mirrors ``CouponDiscountTypeLiteral`` / ``RefundReasonLiteral``.

Cross-field validation lives here (one place):
- ``ends_at`` must be strictly after ``starts_at`` when both are set.

Rendering banners on the storefront is out of scope — this is the data + admin
management layer only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.models.banner import BANNER_PLACEMENT_VALUES
from app.schemas.base import BaseSchema, TimestampSchema

# Hardcoded for pyright; the cross-check below catches drift from the model.
BannerPlacementLiteral = Literal["home_hero", "promo_strip"]

# Import-time drift catch: a placement added to one side without the other fails
# the import (and so every dependent route/test) with a clear message.
assert set(BannerPlacementLiteral.__args__) == set(BANNER_PLACEMENT_VALUES), (  # noqa: S101
    "BannerPlacementLiteral drifted from app.models.banner.BANNER_PLACEMENT_VALUES: "
    f"schema={set(BannerPlacementLiteral.__args__)} vs model={set(BANNER_PLACEMENT_VALUES)}"
)


def validate_window(starts_at: datetime | None, ends_at: datetime | None) -> None:
    """``ends_at`` must be strictly after ``starts_at`` when both are set."""
    if starts_at is not None and ends_at is not None and ends_at <= starts_at:
        raise ValueError("ends_at must be after starts_at")


class BannerBase(BaseSchema):
    """Shared banner fields (creation + response)."""

    title: Annotated[str, Field(min_length=1, max_length=255)]
    subtitle: str | None = Field(default=None, max_length=500)
    image_url: Annotated[str, Field(min_length=1, max_length=2048)]
    link_url: str | None = Field(default=None, max_length=2048)
    placement: BannerPlacementLiteral
    sort_order: int = 0
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def _check_window(self) -> BannerBase:
        validate_window(self.starts_at, self.ends_at)
        return self


class BannerCreate(BannerBase):
    """Request body for ``POST /api/v1/admin/banners``."""


class BannerUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/admin/banners/{id}`` (all fields optional).

    The date-window rule (``ends_at`` > ``starts_at``) is re-validated in the
    route against the **merged** state, because a PATCH may set only one half of
    the pair (e.g. send ``ends_at`` without resending ``starts_at``).
    """

    title: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    subtitle: str | None = Field(default=None, max_length=500)
    image_url: Annotated[str | None, Field(min_length=1, max_length=2048)] = None
    link_url: str | None = Field(default=None, max_length=2048)
    placement: BannerPlacementLiteral | None = None
    sort_order: int | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    is_active: bool | None = None


class BannerResponse(BannerBase, TimestampSchema):
    """A banner row as the API returns it."""


class BannerListResponse(BaseSchema):
    """Paginated banner list."""

    items: list[BannerResponse]
    total: int
    page: int
    page_size: int
    pages: int
