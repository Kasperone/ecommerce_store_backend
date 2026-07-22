"""Tax-category and tax-rate CRUD (no commit; the route owns the transaction).

Mirrors ``CRUDPaymentTerm`` posture. Resolution — ``(category, country, date) →
rate`` — lives in ``app/services/tax.py``; this module is management only.

Two invariants enforced here that the schema can't:
- **Exactly one default category.** A partial unique index guards it at the DB
  level; ``_promote_default`` clears the incumbent first so a new default can be
  set without tripping the index.
- **Rate uniqueness** ``(category, country, valid_from)`` is a DB constraint; the
  route turns its ``IntegrityError`` into a 409.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.product import Product
from app.models.tax import TaxCategory, TaxRate
from app.schemas.tax import (
    TaxCategoryCreate,
    TaxCategoryUpdate,
    TaxRateCreate,
    TaxRateUpdate,
)


class CRUDTaxCategory:
    """Tax categories, with their rates eager-loaded for the response nesting."""

    def get(self, db: Session, id: int) -> TaxCategory | None:
        return db.scalars(
            select(TaxCategory).options(selectinload(TaxCategory.rates)).where(TaxCategory.id == id)
        ).first()

    def get_by_code(self, db: Session, *, code: str) -> TaxCategory | None:
        return db.scalars(select(TaxCategory).where(TaxCategory.code == code)).first()

    def get_multi(self, db: Session) -> list[TaxCategory]:
        """All categories, default first then by id (a small, bounded set)."""
        return list(
            db.scalars(
                select(TaxCategory)
                .options(selectinload(TaxCategory.rates))
                .order_by(TaxCategory.is_default.desc(), TaxCategory.id)
            ).all()
        )

    def _promote_default(self, db: Session, *, keep_id: int | None) -> None:
        """Clear whatever default currently exists (except ``keep_id``), then
        flush — so a new default can be set without violating the single-default
        partial unique index."""
        current = db.scalars(select(TaxCategory).where(TaxCategory.is_default.is_(True))).first()
        if current is not None and current.id != keep_id:
            current.is_default = False
            db.flush()

    def create(self, db: Session, *, obj_in: TaxCategoryCreate) -> TaxCategory:
        if obj_in.is_default:
            self._promote_default(db, keep_id=None)
        row = TaxCategory(
            code=obj_in.code,
            name=obj_in.name,
            name_pl=obj_in.name_pl,
            is_default=obj_in.is_default,
        )
        db.add(row)
        db.flush()
        return row

    def update(self, db: Session, *, db_obj: TaxCategory, obj_in: TaxCategoryUpdate) -> TaxCategory:
        data = obj_in.model_dump(exclude_unset=True)
        if data.get("is_default") is True:
            self._promote_default(db, keep_id=db_obj.id)
        for field, value in data.items():
            setattr(db_obj, field, value)
        db.flush()
        return db_obj

    def delete(self, db: Session, *, db_obj: TaxCategory) -> None:
        db.delete(db_obj)
        db.flush()

    def products_using(self, db: Session, *, category_id: int) -> int:
        """How many products are classified under this category (delete-safety)."""
        return db.query(Product).filter(Product.tax_category_id == category_id).count()


class CRUDTaxRate:
    """Per-country, time-bounded rates under a tax category."""

    def get(self, db: Session, id: int) -> TaxRate | None:
        return db.get(TaxRate, id)

    def get_multi_for_category(self, db: Session, *, category_id: int) -> list[TaxRate]:
        return list(
            db.scalars(
                select(TaxRate)
                .where(TaxRate.tax_category_id == category_id)
                .order_by(TaxRate.country_code, TaxRate.valid_from.desc())
            ).all()
        )

    def find_conflict(
        self,
        db: Session,
        *,
        category_id: int,
        country_code: str,
        valid_from: date,
        exclude_id: int | None = None,
    ) -> TaxRate | None:
        """An existing rate that would collide on the ``(category, country,
        valid_from)`` unique constraint — checked up front so the route can 409
        cleanly instead of catching an IntegrityError mid-savepoint."""
        stmt = select(TaxRate).where(
            TaxRate.tax_category_id == category_id,
            TaxRate.country_code == country_code,
            TaxRate.valid_from == valid_from,
        )
        if exclude_id is not None:
            stmt = stmt.where(TaxRate.id != exclude_id)
        return db.scalars(stmt).first()

    def create(self, db: Session, *, category_id: int, obj_in: TaxRateCreate) -> TaxRate:
        row = TaxRate(
            tax_category_id=category_id,
            country_code=obj_in.country_code,
            rate_pct=obj_in.rate_pct,
            valid_from=obj_in.valid_from,
            valid_to=obj_in.valid_to,
        )
        db.add(row)
        db.flush()
        return row

    def update(self, db: Session, *, db_obj: TaxRate, obj_in: TaxRateUpdate) -> TaxRate:
        data = obj_in.model_dump(exclude_unset=True)
        for field, value in data.items():
            setattr(db_obj, field, value)
        db.flush()
        return db_obj

    def delete(self, db: Session, *, db_obj: TaxRate) -> None:
        db.delete(db_obj)
        db.flush()


tax_category = CRUDTaxCategory()
tax_rate = CRUDTaxRate()
