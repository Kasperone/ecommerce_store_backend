"""Campaign CRUD — admin-managed marketing email campaigns (management only).

Mirrors ``CRUDCoupon``: no commit anywhere (the route owns the transaction so the
mutation and its audit row land together). Sending the campaign / stamping
``sent_at`` is out of scope and lives in the future send layer — the admin CRUD
never sets ``sent_at``.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.campaign import Campaign
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


campaign = CRUDCampaign()
