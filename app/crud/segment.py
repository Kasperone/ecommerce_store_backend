"""Customer segment CRUD + set-based membership resolver.

Mirrors ``CRUDCampaign``: no commit anywhere (the route owns the transaction so
the mutation and its audit row land together).

The resolver (``resolve_user_ids`` / ``preview``) is **set-based SQL** — a single
filtered ``users`` query with EXISTS subqueries (tags, b2b membership) and
correlated scalar subqueries over grouped order aggregates (order count, spend,
last-order) — **not** a per-customer Python loop. The aggregate shapes mirror
``crud_user.order_metrics`` / ``order_counts_for``; the tag/role/active filters
mirror ``crud_user._admin_filtered_query``.
"""

from __future__ import annotations

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session, selectinload

from app.models.company_user import CompanyUser
from app.models.order import Order, OrderStatus
from app.models.segment import Segment
from app.models.tag import Tag, customer_tags
from app.models.user import User, UserRole
from app.schemas.segment import SegmentCreate, SegmentFilters, SegmentUpdate


class CRUDSegment:
    """Segment rows + resolver (no commit; caller owns the txn)."""

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get(self, db: Session, id: int) -> Segment | None:
        return db.get(Segment, id)

    def get_multi(
        self,
        db: Session,
        *,
        is_active: bool | None = None,
        search: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[Segment]:
        query = self._filtered(db, is_active=is_active, search=search)
        return (
            query.order_by(Segment.created_at.desc(), Segment.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count(
        self,
        db: Session,
        *,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> int:
        return self._filtered(db, is_active=is_active, search=search).count()

    def _filtered(self, db: Session, *, is_active: bool | None, search: str | None):
        query = db.query(Segment)
        if is_active is not None:
            query = query.filter(Segment.is_active.is_(is_active))
        if search:
            query = query.filter(Segment.name.ilike(f"%{search.strip()}%"))
        return query

    def create(self, db: Session, *, obj_in: SegmentCreate) -> Segment:
        """Stage a new segment (id populated via flush; caller commits)."""
        segment = Segment(
            name=obj_in.name,
            description=obj_in.description,
            filters=obj_in.filters.model_dump(mode="json"),
            is_active=obj_in.is_active,
        )
        db.add(segment)
        db.flush()
        db.refresh(segment)
        return segment

    def update(self, db: Session, *, segment: Segment, obj_in: SegmentUpdate) -> Segment:
        """Apply a partial update in place (no flush — the audit writer reads
        ``get_history`` between this and the caller's commit). ``filters`` is
        re-serialized to plain JSON when present."""
        data = obj_in.model_dump(exclude_unset=True)
        if "filters" in data and obj_in.filters is not None:
            data["filters"] = obj_in.filters.model_dump(mode="json")
        for field, value in data.items():
            setattr(segment, field, value)
        db.add(segment)
        return segment

    def delete(self, db: Session, *, segment: Segment) -> None:
        db.delete(segment)

    # ------------------------------------------------------------------
    # Set-based resolver
    # ------------------------------------------------------------------

    def _resolve_query(self, filters: SegmentFilters):
        """Build the filtered ``users`` SELECT for a set of segment filters.

        Returns a SQLAlchemy ``Select`` over ``User.id`` with all predicates
        AND-combined. An empty filter object yields every user. Everything is
        expressed as EXISTS / correlated scalar subqueries — no Python looping.
        """
        conditions = []

        # --- tags (customer_tags join) ---
        if filters.tag_ids:
            if filters.tag_match == "all":
                # Customer must carry EVERY listed tag: a grouped subquery over
                # customer_tags counting DISTINCT matched tags == len(tag_ids).
                tag_count = (
                    select(func.count(func.distinct(customer_tags.c.tag_id)))
                    .where(
                        customer_tags.c.user_id == User.id,
                        customer_tags.c.tag_id.in_(filters.tag_ids),
                    )
                    .correlate(User)
                    .scalar_subquery()
                )
                conditions.append(tag_count == len(set(filters.tag_ids)))
            else:
                # "any": EXISTS at least one matching tag link.
                conditions.append(
                    User.tags.any(Tag.id.in_(filters.tag_ids)),
                )

        # --- order count (all statuses) ---
        if filters.min_order_count is not None or filters.max_order_count is not None:
            order_count = (
                select(func.count(Order.id))
                .where(Order.user_id == User.id)
                .correlate(User)
                .scalar_subquery()
            )
            if filters.min_order_count is not None:
                conditions.append(order_count >= filters.min_order_count)
            if filters.max_order_count is not None:
                conditions.append(order_count <= filters.max_order_count)

        # --- total spent in a currency (excludes cancelled) ---
        if filters.min_total_spent is not None:
            # currency is guaranteed present by the schema validator.
            spend = (
                select(func.coalesce(func.sum(Order.total), 0.0))
                .where(
                    Order.user_id == User.id,
                    Order.currency == filters.currency,
                    Order.status != OrderStatus.CANCELLED,
                )
                .correlate(User)
                .scalar_subquery()
            )
            conditions.append(spend >= filters.min_total_spent)

        # --- last order date window (most recent order across all statuses) ---
        if filters.last_order_before is not None or filters.last_order_after is not None:
            last_order = (
                select(func.max(Order.created_at))
                .where(Order.user_id == User.id)
                .correlate(User)
                .scalar_subquery()
            )
            if filters.last_order_after is not None:
                conditions.append(last_order >= filters.last_order_after)
            if filters.last_order_before is not None:
                # Inclusive of the whole `before` day: < (before + 1 day) would
                # require date math; callers pass a date, treat as <= end-of-day
                # by comparing against the date directly (timestamps on that day
                # are >= the date midnight, so use < next-day semantics via the
                # raw date — kept simple/inclusive by `<=` against the date+max).
                conditions.append(func.date(last_order) <= filters.last_order_before)

        # --- country (default shipping address) ---
        if filters.country:
            from app.models.address import Address

            conditions.append(
                exists(
                    select(Address.id).where(
                        Address.user_id == User.id,
                        Address.is_default_shipping.is_(True),
                        Address.country == filters.country,
                    )
                )
            )

        # --- role ---
        if filters.role == "customer":
            conditions.append(User.role == UserRole.CUSTOMER)
        elif filters.role == "b2b":
            conditions.append(exists(select(CompanyUser.id).where(CompanyUser.user_id == User.id)))

        stmt = select(User.id)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        return stmt

    def resolve_user_ids(self, db: Session, filters: SegmentFilters) -> set[int]:
        """Resolve a filter to the set of matching user ids (set-based SQL)."""
        rows = db.execute(self._resolve_query(filters)).scalars().all()
        return set(rows)

    def preview(
        self, db: Session, filters: SegmentFilters, *, limit: int = 20
    ) -> tuple[int, list[User]]:
        """Return ``(count, sample)`` for a filter.

        ``count`` is the full match total (set-based COUNT); ``sample`` is a
        page of matching ``User`` rows (newest first), tags eager-loaded for the
        ``CustomerListItem`` chips. One COUNT query + one page query — no loops.

        ``limit=0`` is a legitimate *count-only* request: the sample query emits
        ``LIMIT 0`` and returns nothing, while ``count`` is unaffected (it is a
        separate COUNT). Callers that render "N recipients" without a preview
        list use it.
        """
        base = self._resolve_query(filters).subquery()
        count = db.execute(select(func.count()).select_from(base)).scalar_one()

        sample = (
            db.query(User)
            .filter(User.id.in_(select(self._resolve_query(filters).subquery().c.id)))
            .options(selectinload(User.tags))
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(limit)
            .all()
        )
        return count, sample


segment = CRUDSegment()
