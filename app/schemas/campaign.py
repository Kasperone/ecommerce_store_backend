"""Pydantic schemas for admin campaign management.

``CampaignAudienceLiteral`` / ``CampaignStatusLiteral`` are hardcoded (pyright
needs literal arguments in type positions) but cross-checked against the model's
``CAMPAIGN_AUDIENCE_VALUES`` / ``CAMPAIGN_STATUS_VALUES`` at import time, so the
DB CHECK, the schema, and the route can't drift apart silently. Mirror
``CouponDiscountTypeLiteral`` / ``RefundReasonLiteral``.

Cross-field validation lives here (one place):
- ``scheduled_at`` is required when ``status`` is ``scheduled``.

Sending the campaign is out of scope, so there is no "send" schema and
``sent_at`` is read-only on the response (the future send layer stamps it).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.models.campaign import CAMPAIGN_AUDIENCE_VALUES, CAMPAIGN_STATUS_VALUES
from app.schemas.base import BaseSchema, TimestampSchema

# Hardcoded for pyright; the cross-checks below catch drift from the model.
CampaignAudienceLiteral = Literal["all", "customers", "b2b", "segment"]
CampaignStatusLiteral = Literal["draft", "scheduled", "sent"]

# Import-time drift catch: an enum value added to one side without the other
# fails the import (and so every dependent route/test) with a clear message.
assert set(CampaignAudienceLiteral.__args__) == set(CAMPAIGN_AUDIENCE_VALUES), (  # noqa: S101
    "CampaignAudienceLiteral drifted from app.models.campaign.CAMPAIGN_AUDIENCE_VALUES: "
    f"schema={set(CampaignAudienceLiteral.__args__)} vs model={set(CAMPAIGN_AUDIENCE_VALUES)}"
)
assert set(CampaignStatusLiteral.__args__) == set(CAMPAIGN_STATUS_VALUES), (  # noqa: S101
    "CampaignStatusLiteral drifted from app.models.campaign.CAMPAIGN_STATUS_VALUES: "
    f"schema={set(CampaignStatusLiteral.__args__)} vs model={set(CAMPAIGN_STATUS_VALUES)}"
)


def validate_schedule(status: str, scheduled_at: datetime | None) -> None:
    """A ``scheduled`` campaign must have a ``scheduled_at``. Raises ``ValueError``."""
    if status == "scheduled" and scheduled_at is None:
        raise ValueError("scheduled campaigns require a scheduled_at")


def validate_segment_audience(audience: str, segment_id: int | None) -> None:
    """Enforce ``audience='segment'`` ⟺ ``segment_id`` is set (mirrors the DB CHECK).

    A ``segment`` campaign requires a ``segment_id``; any other audience must not
    carry one. Raises ``ValueError`` on violation."""
    if audience == "segment" and segment_id is None:
        raise ValueError("segment campaigns require a segment_id")
    if audience != "segment" and segment_id is not None:
        raise ValueError("segment_id is only allowed when audience is 'segment'")


class CampaignBase(BaseSchema):
    """Shared campaign fields (creation + response).

    ``status`` defaults to ``draft``. ``sent_at`` is intentionally absent here —
    it's read-only and only appears on the response (see ``CampaignResponse``).
    """

    name: Annotated[str, Field(min_length=1, max_length=255)]
    subject: Annotated[str, Field(min_length=1, max_length=255)]
    audience: CampaignAudienceLiteral
    status: CampaignStatusLiteral = "draft"
    body: str | None = None
    scheduled_at: datetime | None = None
    segment_id: int | None = None

    @model_validator(mode="after")
    def _check_schedule(self) -> CampaignBase:
        validate_schedule(self.status, self.scheduled_at)
        validate_segment_audience(self.audience, self.segment_id)
        return self


class CampaignCreate(CampaignBase):
    """Request body for ``POST /api/v1/admin/campaigns``."""


class CampaignUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/admin/campaigns/{id}`` (all fields optional).

    The ``scheduled``-requires-``scheduled_at`` rule is re-validated in the route
    against the **merged** state, because a PATCH may set ``status=scheduled``
    without resending ``scheduled_at`` (or clear ``scheduled_at`` on an
    already-scheduled campaign). ``sent_at`` is intentionally absent — it's
    managed by the future send layer, not the admin form.
    """

    name: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    subject: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    audience: CampaignAudienceLiteral | None = None
    status: CampaignStatusLiteral | None = None
    body: str | None = None
    scheduled_at: datetime | None = None
    segment_id: int | None = None


class CampaignResponse(CampaignBase, TimestampSchema):
    """A campaign row as the API returns it."""

    sent_at: datetime | None = None


class CampaignListResponse(BaseSchema):
    """Paginated campaign list."""

    items: list[CampaignResponse]
    total: int
    page: int
    page_size: int
    pages: int
