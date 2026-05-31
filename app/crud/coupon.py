"""Coupon CRUD — admin-managed discount codes (management only).

Mirrors ``CRUDWarehouse``: no commit anywhere (the route owns the transaction so
the mutation and its audit row land together). ``code`` is matched case-insensitively
via an uppercased comparison — the schema already uppercases on the way in, so the
stored value is canonical, and ``get_by_code`` uppercases the lookup argument
defensively. Redemption / ``used_count`` bumping is out of scope and lives elsewhere.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.coupon import Coupon
from app.schemas.coupon import CouponCreate, CouponUpdate


class CRUDCoupon:
    """Coupon rows (no commit; caller owns the txn)."""

    def get(self, db: Session, id: int) -> Coupon | None:
        return db.get(Coupon, id)

    def get_by_code(self, db: Session, *, code: str) -> Coupon | None:
        """Look up by code (case-insensitive — codes are stored UPPERCASE)."""
        return db.query(Coupon).filter(Coupon.code == code.strip().upper()).first()

    def get_multi(
        self,
        db: Session,
        *,
        is_active: bool | None = None,
        search: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[Coupon]:
        query = self._filtered(db, is_active=is_active, search=search)
        return (
            query.order_by(Coupon.created_at.desc(), Coupon.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count(
        self, db: Session, *, is_active: bool | None = None, search: str | None = None
    ) -> int:
        return self._filtered(db, is_active=is_active, search=search).count()

    def _filtered(self, db: Session, *, is_active: bool | None, search: str | None):
        query = db.query(Coupon)
        if is_active is not None:
            query = query.filter(Coupon.is_active == is_active)
        if search:
            query = query.filter(Coupon.code.ilike(f"%{search.strip()}%"))
        return query

    def create(self, db: Session, *, obj_in: CouponCreate) -> Coupon:
        """Stage a new coupon (id populated via flush; caller commits)."""
        coupon = Coupon(
            code=obj_in.code,
            description=obj_in.description,
            discount_type=obj_in.discount_type,
            value=obj_in.value,
            currency=obj_in.currency,
            min_order_amount=obj_in.min_order_amount,
            max_uses=obj_in.max_uses,
            starts_at=obj_in.starts_at,
            ends_at=obj_in.ends_at,
            is_active=obj_in.is_active,
        )
        db.add(coupon)
        db.flush()
        db.refresh(coupon)
        return coupon

    def update(self, db: Session, *, coupon: Coupon, obj_in: CouponUpdate) -> Coupon:
        """Apply a partial update (no flush — the audit writer reads ``get_history``
        between this and the caller's commit). Only fields explicitly sent are
        touched; ``used_count`` is never settable here."""
        data = obj_in.model_dump(exclude_unset=True)
        for field, value in data.items():
            setattr(coupon, field, value)
        db.add(coupon)
        return coupon

    def delete(self, db: Session, *, coupon: Coupon) -> None:
        db.delete(coupon)


coupon = CRUDCoupon()
