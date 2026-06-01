"""Factory-boy factory for Campaign.

Session binding follows the same pattern as ``CouponFactory`` — populated
dynamically by a per-test fixture. Defaults to a draft campaign for the ``all``
audience; pass ``status="scheduled", scheduled_at=...`` for a scheduled one.
"""

from __future__ import annotations

import factory
from factory.alchemy import SQLAlchemyModelFactory

from app.models.campaign import Campaign


class CampaignFactory(SQLAlchemyModelFactory):
    class Meta:  # pyright: ignore[reportIncompatibleVariableOverride] -- factory-boy idiom; Meta is the documented config hook
        model = Campaign
        sqlalchemy_session = None  # populated by the campaign_factory fixture
        sqlalchemy_session_persistence = "commit"

    name = factory.Sequence(lambda n: f"Campaign {n}")
    subject = factory.Sequence(lambda n: f"Subject {n}")
    audience = "all"
    status = "draft"
    body = None
    scheduled_at = None
    sent_at = None
