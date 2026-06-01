"""Campaign CRUD — admin-managed marketing email campaigns.

Mirrors ``CRUDCoupon``: no commit anywhere (the route owns the transaction so the
mutation and its audit row land together). The send layer (``POST
/admin/campaigns/{id}/send``) lives in the route — this module only provides the
audience-resolution + ``mark_sent`` helpers it leans on; neither commits.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.company import Company
from app.models.user import User, UserRole
from app.schemas.campaign import CampaignCreate, CampaignUpdate


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

    def resolve_recipient_emails(self, db: Session, *, audience: str) -> list[str]:
        """Resolve a campaign ``audience`` to a list of recipient email addresses.

        - ``all`` → every ``User`` with a non-empty email.
        - ``customers`` → users whose ``role`` is ``CUSTOMER`` (non-empty email).
        - ``b2b`` → distinct, non-null ``companies.email``.

        Empty / NULL emails are filtered out so the send loop never hands a blank
        address to the transport.
        """
        if audience == "b2b":
            rows = (
                db.query(Company.email)
                .filter(Company.email.isnot(None), Company.email != "")
                .distinct()
                .all()
            )
            return [email for (email,) in rows if email]

        query = db.query(User.email).filter(User.email.isnot(None), User.email != "")
        if audience == "customers":
            query = query.filter(User.role == UserRole.CUSTOMER)
        return [email for (email,) in query.all() if email]

    def mark_sent(self, db: Session, *, campaign: Campaign) -> Campaign:
        """Flip a campaign to ``sent`` and stamp ``sent_at`` (naive UTC).

        No commit — the route owns the transaction so the status flip and its
        audit row land together (mirrors the rest of this module)."""
        campaign.status = "sent"
        campaign.sent_at = datetime.now(UTC).replace(tzinfo=None)
        db.add(campaign)
        return campaign


campaign = CRUDCampaign()
