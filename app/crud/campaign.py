"""Campaign CRUD — admin-managed marketing email campaigns.

Mirrors ``CRUDCoupon``: no commit anywhere (the route owns the transaction so the
mutation and its audit row land together). The send layer (``POST
/admin/campaigns/{id}/send``) lives in the route — this module only provides the
audience-resolution + ``mark_sent`` helpers it leans on; neither commits.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.crud.email_suppression import email_suppression as crud_email_suppression
from app.crud.segment import segment as crud_segment
from app.crud.user_consent import user_consent as crud_user_consent
from app.models.campaign import Campaign
from app.models.company import Company
from app.models.segment import Segment
from app.models.user import User, UserRole
from app.models.user_consent import MARKETING_CONSENT_TYPES
from app.schemas.campaign import CampaignCreate, CampaignUpdate
from app.schemas.segment import SegmentFilters


class CRUDCampaign:
    """Campaign rows (no commit; caller owns the txn)."""

    def get(self, db: Session, id: int) -> Campaign | None:
        return db.get(Campaign, id)

    def get_multi(
        self,
        db: Session,
        *,
        status: str | None = None,
        audience: str | None = None,
        search: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[Campaign]:
        query = self._filtered(db, status=status, audience=audience, search=search)
        return (
            query.order_by(Campaign.created_at.desc(), Campaign.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count(
        self,
        db: Session,
        *,
        status: str | None = None,
        audience: str | None = None,
        search: str | None = None,
    ) -> int:
        return self._filtered(db, status=status, audience=audience, search=search).count()

    def _filtered(
        self, db: Session, *, status: str | None, audience: str | None, search: str | None
    ):
        query = db.query(Campaign)
        if status is not None:
            query = query.filter(Campaign.status == status)
        if audience is not None:
            query = query.filter(Campaign.audience == audience)
        if search:
            query = query.filter(Campaign.name.ilike(f"%{search.strip()}%"))
        return query

    def create(self, db: Session, *, obj_in: CampaignCreate) -> Campaign:
        """Stage a new campaign (id populated via flush; caller commits).

        ``sent_at`` is never set here — it defaults to NULL and is the future
        send layer's responsibility.
        """
        campaign = Campaign(
            name=obj_in.name,
            subject=obj_in.subject,
            audience=obj_in.audience,
            status=obj_in.status,
            body=obj_in.body,
            scheduled_at=obj_in.scheduled_at,
            segment_id=obj_in.segment_id,
        )
        db.add(campaign)
        db.flush()
        db.refresh(campaign)
        return campaign

    def update(self, db: Session, *, campaign: Campaign, obj_in: CampaignUpdate) -> Campaign:
        """Apply a partial update (no flush — the audit writer reads ``get_history``
        between this and the caller's commit). Only fields explicitly sent are
        touched; ``sent_at`` is never settable here."""
        data = obj_in.model_dump(exclude_unset=True)
        for field, value in data.items():
            setattr(campaign, field, value)
        db.add(campaign)
        return campaign

    def delete(self, db: Session, *, campaign: Campaign) -> None:
        db.delete(campaign)

    def resolve_recipient_emails(
        self, db: Session, *, audience: str, segment_id: int | None = None
    ) -> list[str]:
        """Resolve a campaign ``audience`` to the addresses we may lawfully mail.

        Audience shapes:

        - ``all`` → every active ``User`` with a non-empty email.
        - ``customers`` → the same, narrowed to ``role == CUSTOMER``.
        - ``b2b`` → distinct, non-null ``companies.email``.
        - ``segment`` → the ``User`` rows resolved by the referenced segment's
          typed filters (``crud_segment.resolve_user_ids``). If ``segment_id``
          is missing or the segment row no longer exists (e.g. it was deleted →
          ``ON DELETE SET NULL`` nulled the FK), the result is empty — the send
          route then mails nobody.

        **Three filters apply on top of the shape**, and every one of them is
        load-bearing:

        1. **Explicit marketing consent** (user audiences only). A campaign is
           unsolicited commercial mail unless the recipient opted in; under
           UŚUDE art. 10 / GDPR Art. 6(1)(a) silence is not consent. This is the
           same gate ``send_abandoned_cart_emails`` has always applied — the
           campaign sender simply never had it. **Expect the addressable
           audience to shrink sharply the first time this runs:** users who
           predate the account-preferences toggle have no consent row and are
           correctly excluded until they opt in.
        2. **Active accounts only.** ``anonymize`` (GDPR erasure) sets
           ``is_active = False`` and rewrites the address to
           ``deleted-user-<id>@anon.invalid`` — non-empty, so the old
           non-empty check happily mailed it. Every such send is a guaranteed
           hard bounce, and hard bounces are what get a sending domain
           throttled, taking the *transactional* mail down with it.
        3. **The suppression list.** Anyone who used an unsubscribe link is
           excluded regardless of what their consent rows say — a later opt-in
           record must never silently resurrect a stopped address.

        ``b2b`` is deliberately **not** consent-gated: ``companies.email`` is a
        business contact address supplied for the trading relationship and there
        is no per-company consent record to check. It is still suppression-
        filtered and still carries an unsubscribe link. If the store ever mails
        B2B prospects it did not trade with, this needs a real opt-in field on
        ``Company`` — the current posture only holds for existing customers.
        """
        if audience == "b2b":
            rows = (
                db.query(Company.email)
                .filter(Company.email.isnot(None), Company.email != "")
                .distinct()
                .all()
            )
            emails = [email for (email,) in rows if email]
            return crud_email_suppression.filter_allowed(db, emails=emails)

        query = db.query(User.id, User.email).filter(
            User.is_active.is_(True),
            User.email.isnot(None),
            User.email != "",
        )
        if audience == "segment":
            if segment_id is None:
                return []
            segment = db.get(Segment, segment_id)
            if segment is None:
                return []
            user_ids = crud_segment.resolve_user_ids(
                db, SegmentFilters.model_validate(segment.filters)
            )
            if not user_ids:
                return []
            query = query.filter(User.id.in_(user_ids))
        elif audience == "customers":
            query = query.filter(User.role == UserRole.CUSTOMER)

        candidates = [(uid, email) for uid, email in query.all() if email]
        if not candidates:
            return []

        consented = crud_user_consent.granted_user_ids(
            db,
            user_ids=[uid for uid, _ in candidates],
            consent_types=MARKETING_CONSENT_TYPES,
        )
        emails = [email for uid, email in candidates if uid in consented]
        return crud_email_suppression.filter_allowed(db, emails=emails)

    def mark_sent(self, db: Session, *, campaign: Campaign) -> Campaign:
        """Flip a campaign to ``sent`` and stamp ``sent_at`` (naive UTC).

        No commit — the route owns the transaction so the status flip and its
        audit row land together (mirrors the rest of this module)."""
        campaign.status = "sent"
        campaign.sent_at = datetime.now(UTC).replace(tzinfo=None)
        db.add(campaign)
        return campaign


campaign = CRUDCampaign()
