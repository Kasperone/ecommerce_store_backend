"""Company CRUD — admin-managed B2B company directory (management only).

Mirrors ``CRUDCampaign`` / ``CRUDCoupon``: no commit anywhere (the route owns the
transaction so the mutation and its audit row land together). An approval
workflow beyond the status field, and linking users to companies, are out of
scope and live elsewhere.

``search`` matches a substring (case-insensitive) on either ``name`` or
``tax_id``.
"""

from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.user import User
from app.schemas.company import CompanyCreate, CompanyUpdate


class CRUDCompany:
    """Company rows (no commit; caller owns the txn)."""

    def get(self, db: Session, id: int) -> Company | None:
        return db.get(Company, id)

    def user_exists(self, db: Session, user_id: int) -> bool:
        """True if a user with ``user_id`` exists (route 422s on False when a
        ``primary_user_id`` is supplied)."""
        return db.query(User.id).filter(User.id == user_id).first() is not None

    def get_multi(
        self,
        db: Session,
        *,
        status: str | None = None,
        search: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[Company]:
        query = self._filtered(db, status=status, search=search)
        return (
            query.order_by(Company.created_at.desc(), Company.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count(self, db: Session, *, status: str | None = None, search: str | None = None) -> int:
        return self._filtered(db, status=status, search=search).count()

    def _filtered(self, db: Session, *, status: str | None, search: str | None):
        query = db.query(Company)
        if status is not None:
            query = query.filter(Company.status == status)
        if search:
            term = f"%{search.strip()}%"
            query = query.filter(or_(Company.name.ilike(term), Company.tax_id.ilike(term)))
        return query

    def create(self, db: Session, *, obj_in: CompanyCreate) -> Company:
        """Stage a new company (id populated via flush; caller commits)."""
        company = Company(
            name=obj_in.name,
            tax_id=obj_in.tax_id,
            email=obj_in.email,
            phone=obj_in.phone,
            status=obj_in.status,
            account_manager=obj_in.account_manager,
            notes=obj_in.notes,
            primary_user_id=obj_in.primary_user_id,
        )
        db.add(company)
        db.flush()
        db.refresh(company)
        return company

    def update(self, db: Session, *, company: Company, obj_in: CompanyUpdate) -> Company:
        """Apply a partial update (no flush — the audit writer reads ``get_history``
        between this and the caller's commit). Only fields explicitly sent are
        touched."""
        data = obj_in.model_dump(exclude_unset=True)
        for field, value in data.items():
            setattr(company, field, value)
        db.add(company)
        return company

    def delete(self, db: Session, *, company: Company) -> None:
        db.delete(company)


company = CRUDCompany()
