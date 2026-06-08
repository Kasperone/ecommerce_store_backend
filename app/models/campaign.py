"""Campaign model — admin-managed marketing email campaigns.

This is **management metadata only** (same posture as coupons / banners): a
campaign records the email's name, subject, target audience, optional body, an
optional schedule, and a lifecycle status. **Actually sending the email /
executing the audience segmentation is deliberately NOT modelled here** — that
is a separate, deferred concern. The admin CRUD only manages the rows;
``sent_at`` is a read-only column the future send layer will stamp.

``audience`` and ``status`` follow the ``RefundReason`` / ``CouponDiscountType``
convention: a ``str``-enum stored as the lowercase value in a plain
``String(16)`` column (not a PG enum), guarded by a CHECK constraint. Adding a
value later is a code-only change with no migration.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.segment import Segment


class CampaignAudience(str, enum.Enum):
    """Who a campaign targets.

    Stored as the lowercase value in a ``String(16)`` column (mirrors
    ``RefundReason``). ``ALL`` is every user; ``CUSTOMERS`` is the retail
    customer base; ``B2B`` is business accounts; ``SEGMENT`` targets the
    customers resolved by a referenced :class:`~app.models.segment.Segment`
    (``segment_id`` then required — see the table CHECK).
    """

    ALL = "all"
    CUSTOMERS = "customers"
    B2B = "b2b"
    SEGMENT = "segment"


class CampaignStatus(str, enum.Enum):
    """A campaign's lifecycle stage.

    Stored as the lowercase value in a ``String(16)`` column. ``DRAFT`` is the
    default for a freshly created campaign; ``SCHEDULED`` means it has a
    ``scheduled_at`` and is queued for the future send layer; ``SENT`` is set by
    that layer once delivery completes (alongside ``sent_at``).
    """

    DRAFT = "draft"
    SCHEDULED = "scheduled"
    SENT = "sent"


# Mirrors of the enum values, used by the DB CHECK constraints and referenced by
# ``app/schemas/campaign.py`` so the route layer, DB layer, and OpenAPI schema
# can never drift apart. Mirror ``REFUND_REASON_VALUES``.
CAMPAIGN_AUDIENCE_VALUES: tuple[str, ...] = tuple(a.value for a in CampaignAudience)
CAMPAIGN_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in CampaignStatus)


class Campaign(BaseModel):
    """An admin-managed marketing email campaign.

    ``body`` (the email content) and ``scheduled_at`` are optional. ``status``
    defaults to ``draft``; the schema enforces that ``scheduled_at`` is present
    when ``status`` is ``scheduled``. ``sent_at`` is read-only here — the admin
    CRUD never sets it; the future send layer stamps it (both datetime columns
    are naive UTC, like every datetime column in this app).
    """

    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint(
            "audience IN ('" + "', '".join(CAMPAIGN_AUDIENCE_VALUES) + "')",
            name="ck_campaigns_audience_enum",
        ),
        CheckConstraint(
            "status IN ('" + "', '".join(CAMPAIGN_STATUS_VALUES) + "')",
            name="ck_campaigns_status_enum",
        ),
        # ``segment_id`` may only be set when ``audience='segment'`` — a
        # non-segment campaign must never carry one. This is the DB half of the
        # invariant; the *other* half (a segment campaign REQUIRES a segment_id)
        # is enforced at the schema/route layer at write time, NOT here.
        # Deliberately one-directional so ``ON DELETE SET NULL`` can null the FK
        # when the referenced segment is deleted without tripping the CHECK on
        # the resulting (audience='segment', segment_id=NULL) orphan row — that
        # degraded state is intended (the campaign then resolves to no
        # recipients; see ``crud_campaign.resolve_recipient_emails``).
        CheckConstraint(
            "segment_id IS NULL OR audience = 'segment'",
            name="ck_campaigns_segment_audience",
        ),
    )

    name: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    audience: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")
    body: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Set only when ``audience='segment'``. ON DELETE SET NULL: deleting the
    # referenced segment nulls this out (the campaign no longer resolves a
    # segment audience — the route/resolver then treats it as having no
    # recipients; see ``resolve_recipient_emails``).
    segment_id: Mapped[int | None] = mapped_column(
        ForeignKey("segments.id", ondelete="SET NULL"), nullable=True
    )

    segment: Mapped[Segment | None] = relationship("Segment")

    def __repr__(self) -> str:
        return f"<Campaign {self.id} {self.status!r} {self.name!r}>"
