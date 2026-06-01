"""Banner CRUD — admin-managed storefront banners (management only).

Mirrors ``CRUDCoupon``: no commit anywhere (the route owns the transaction so the
mutation and its audit row land together). Rendering banners on the storefront is
out of scope and lives elsewhere.

List ordering is ``sort_order`` ascending (lower first within a slot), then newest
first as a stable tie-breaker.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.banner import Banner
from app.schemas.banner import BannerCreate, BannerUpdate


class CRUDBanner:
    """Banner rows (no commit; caller owns the txn)."""

    def get(self, db: Session, id: int) -> Banner | None:
        return db.get(Banner, id)

    def get_multi(
        self,
        db: Session,
        *,
        is_active: bool | None = None,
        placement: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[Banner]:
        query = self._filtered(db, is_active=is_active, placement=placement)
        return (
            query.order_by(Banner.sort_order.asc(), Banner.created_at.desc(), Banner.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count(
        self, db: Session, *, is_active: bool | None = None, placement: str | None = None
    ) -> int:
        return self._filtered(db, is_active=is_active, placement=placement).count()

    def _filtered(self, db: Session, *, is_active: bool | None, placement: str | None):
        query = db.query(Banner)
        if is_active is not None:
            query = query.filter(Banner.is_active == is_active)
        if placement is not None:
            query = query.filter(Banner.placement == placement)
        return query

    def get_active(self, db: Session, *, placement: str | None = None) -> list[Banner]:
        """Public storefront read: banners that are active AND currently within
        their optional display window.

        A null ``starts_at`` is open-ended on the lower bound; a null ``ends_at``
        is open-ended on the upper bound. Comparison is against naive UTC ``now``
        (every datetime column in this app is naive UTC). Ordered ``sort_order``
        ascending then ``id`` ascending — a stable, deterministic public order.
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        query = db.query(Banner).filter(
            Banner.is_active.is_(True),
            or_(Banner.starts_at.is_(None), Banner.starts_at <= now),
            or_(Banner.ends_at.is_(None), Banner.ends_at >= now),
        )
        if placement is not None:
            query = query.filter(Banner.placement == placement)
        return query.order_by(Banner.sort_order.asc(), Banner.id.asc()).all()

    def create(self, db: Session, *, obj_in: BannerCreate) -> Banner:
        """Stage a new banner (id populated via flush; caller commits)."""
        banner = Banner(
            title=obj_in.title,
            subtitle=obj_in.subtitle,
            image_url=obj_in.image_url,
            link_url=obj_in.link_url,
            placement=obj_in.placement,
            sort_order=obj_in.sort_order,
            starts_at=obj_in.starts_at,
            ends_at=obj_in.ends_at,
            is_active=obj_in.is_active,
        )
        db.add(banner)
        db.flush()
        db.refresh(banner)
        return banner

    def update(self, db: Session, *, banner: Banner, obj_in: BannerUpdate) -> Banner:
        """Apply a partial update (no flush — the audit writer reads ``get_history``
        between this and the caller's commit). Only fields explicitly sent are
        touched."""
        data = obj_in.model_dump(exclude_unset=True)
        for field, value in data.items():
            setattr(banner, field, value)
        db.add(banner)
        return banner

    def delete(self, db: Session, *, banner: Banner) -> None:
        db.delete(banner)


banner = CRUDBanner()
