"""Payment-term CRUD — admin-managed B2B net-terms templates (net-terms tail Program A).

Mirrors ``CRUDPriceList`` posture: no commit anywhere (the route owns the
transaction). Management only — resolution lives in
``app/services/b2b_net_terms.py`` (the checkout read path). ``term_type`` and its
required parameter (``net_days`` / ``due_date``) are validated at the schema
layer; ``create``/``update`` trust the validated payload.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.payment_term import PaymentTerm
from app.schemas.payment_term import PaymentTermCreate, PaymentTermUpdate


class CRUDPaymentTerm:
    """Payment-term rows (no commit; caller owns the txn)."""

    def get(self, db: Session, id: int) -> PaymentTerm | None:
        return db.get(PaymentTerm, id)

    def get_multi(
        self, db: Session, *, search: str | None = None, skip: int = 0, limit: int = 100
    ) -> list[PaymentTerm]:
        return (
            self._filtered(db, search=search)
            .order_by(PaymentTerm.created_at.desc(), PaymentTerm.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count(self, db: Session, *, search: str | None = None) -> int:
        return self._filtered(db, search=search).count()

    def _filtered(self, db: Session, *, search: str | None):
        query = db.query(PaymentTerm)
        if search:
            query = query.filter(PaymentTerm.name.ilike(f"%{search}%"))
        return query

    def create(self, db: Session, *, obj_in: PaymentTermCreate) -> PaymentTerm:
        row = PaymentTerm(
            name=obj_in.name,
            # ``use_enum_values=True`` on BaseSchema already coerces the enum to
            # its str value on the schema instance.
            term_type=obj_in.term_type,
            net_days=obj_in.net_days,
            due_date=obj_in.due_date,
            is_active=obj_in.is_active,
            notes=obj_in.notes,
        )
        db.add(row)
        db.flush()
        return row

    def update(self, db: Session, *, db_obj: PaymentTerm, obj_in: PaymentTermUpdate) -> PaymentTerm:
        data = obj_in.model_dump(exclude_unset=True)
        # term_type is already a str here (use_enum_values on BaseSchema).
        for field, value in data.items():
            setattr(db_obj, field, value)
        db.flush()
        return db_obj

    def delete(self, db: Session, *, db_obj: PaymentTerm) -> None:
        db.delete(db_obj)
        db.flush()

    def in_use_count(self, db: Session, *, payment_term_id: int) -> int:
        """How many companies are assigned this term (for a delete-safety note)."""
        return db.query(Company).filter(Company.payment_term_id == payment_term_id).count()


payment_term = CRUDPaymentTerm()
