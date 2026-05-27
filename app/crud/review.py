"""Review CRUD.

Customer create / own-review helpers, public read helpers (approved only, with a
ratings summary), and admin moderation list helpers. Inherits ``CRUDBase`` for
``get`` / ``update`` (no-flush, so the audit writer can read the diff) / ``delete``.

Queries that feed a response carrying ``author_name`` / product fields
``selectinload`` the ``user`` / ``product`` relationships to avoid N+1.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from app.crud.base import CRUDBase
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.review import Review, ReviewStatus
from app.models.user import User
from app.schemas.review import ReviewCreate, ReviewUpdate


def _utcnow_naive() -> datetime:
    """UTC now as naive datetime, matching the model's column type."""
    return datetime.now(UTC).replace(tzinfo=None)


class CRUDReview(CRUDBase[Review, ReviewCreate, ReviewUpdate]):
    """Review persistence + the read shapes the public widget and admin list need."""

    # ------------------------------------------------------------------
    # Customer
    # ------------------------------------------------------------------

    def get_by_product_and_user(
        self, db: Session, *, product_id: int, user_id: int
    ) -> Review | None:
        """The caller's existing review for a product, or None (dup-check + own-review)."""
        return (
            db.query(Review)
            .filter(Review.product_id == product_id, Review.user_id == user_id)
            .first()
        )

    def user_has_purchased(self, db: Session, *, user_id: int, product_id: int) -> bool:
        """True if the user has a non-cancelled order containing this product.

        Drives the ``is_verified_purchase`` snapshot taken at review creation.
        """
        row = (
            db.query(OrderItem.id)
            .join(Order, OrderItem.order_id == Order.id)
            .filter(
                Order.user_id == user_id,
                OrderItem.product_id == product_id,
                Order.status != OrderStatus.CANCELLED,
            )
            .first()
        )
        return row is not None

    def create_for_user(
        self,
        db: Session,
        *,
        product_id: int,
        user_id: int,
        obj_in: ReviewCreate,
        is_verified_purchase: bool,
    ) -> Review:
        """Stage a new ``pending`` review; flush so ``id`` is populated. Caller commits."""
        review = Review(
            product_id=product_id,
            user_id=user_id,
            rating=obj_in.rating,
            title=obj_in.title,
            body=obj_in.body,
            status=ReviewStatus.PENDING.value,
            is_verified_purchase=is_verified_purchase,
        )
        db.add(review)
        db.flush()
        db.refresh(review)
        return review

    # ------------------------------------------------------------------
    # Public read (approved only)
    # ------------------------------------------------------------------

    def get_multi_public(
        self, db: Session, *, product_id: int, skip: int = 0, limit: int = 20
    ) -> list[Review]:
        """Approved reviews for a product, newest first, author eager-loaded."""
        return (
            db.query(Review)
            .options(selectinload(Review.user))
            .filter(
                Review.product_id == product_id,
                Review.status == ReviewStatus.APPROVED.value,
            )
            .order_by(Review.created_at.desc(), Review.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_public(self, db: Session, *, product_id: int) -> int:
        return (
            db.query(Review)
            .filter(
                Review.product_id == product_id,
                Review.status == ReviewStatus.APPROVED.value,
            )
            .count()
        )

    def summary(self, db: Session, *, product_id: int) -> tuple[float, int, dict[str, int]]:
        """``(average rounded to 1dp, total, per-star breakdown)`` over approved reviews."""
        rows = (
            db.query(Review.rating, func.count(Review.id))
            .filter(
                Review.product_id == product_id,
                Review.status == ReviewStatus.APPROVED.value,
            )
            .group_by(Review.rating)
            .all()
        )
        breakdown = {str(star): 0 for star in range(1, 6)}
        total = 0
        weighted = 0
        for rating, count in rows:
            breakdown[str(rating)] = count
            total += count
            weighted += rating * count
        average = round(weighted / total, 1) if total else 0.0
        return average, total, breakdown

    # ------------------------------------------------------------------
    # Admin moderation list
    # ------------------------------------------------------------------

    def _admin_query(
        self,
        db: Session,
        *,
        status: str | None,
        product_id: int | None,
        rating: int | None,
        search: str | None,
    ):
        query = (
            db.query(Review)
            .join(Product, Review.product_id == Product.id)
            .join(User, Review.user_id == User.id)
        )
        if status:
            query = query.filter(Review.status == status)
        if product_id is not None:
            query = query.filter(Review.product_id == product_id)
        if rating is not None:
            query = query.filter(Review.rating == rating)
        if search:
            like = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    Review.title.ilike(like),
                    Review.body.ilike(like),
                    User.email.ilike(like),
                    Product.name.ilike(like),
                )
            )
        return query

    def get_multi_admin(
        self,
        db: Session,
        *,
        status: str | None = None,
        product_id: int | None = None,
        rating: int | None = None,
        search: str | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> list[Review]:
        """Filtered moderation list, newest first, with product + author eager-loaded."""
        return (
            self._admin_query(
                db, status=status, product_id=product_id, rating=rating, search=search
            )
            .options(selectinload(Review.user), selectinload(Review.product))
            .order_by(Review.created_at.desc(), Review.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_admin(
        self,
        db: Session,
        *,
        status: str | None = None,
        product_id: int | None = None,
        rating: int | None = None,
        search: str | None = None,
    ) -> int:
        return self._admin_query(
            db, status=status, product_id=product_id, rating=rating, search=search
        ).count()

    def set_admin_response(self, db: Session, *, review: Review, response: str | None) -> Review:
        """Set or clear the public store reply on a review (no flush/commit).

        A non-blank ``response`` is trimmed, stored, and stamps
        ``admin_response_at`` to now; ``None`` or a blank string clears both.
        Left unflushed so the audit writer can read the diff via ``get_history``
        before the route commits — same contract as ``CRUDBase.update``.
        """
        text = (response or "").strip()
        if text:
            review.admin_response = text
            review.admin_response_at = _utcnow_naive()
        else:
            review.admin_response = None
            review.admin_response_at = None
        db.add(review)
        return review


review = CRUDReview(Review)
