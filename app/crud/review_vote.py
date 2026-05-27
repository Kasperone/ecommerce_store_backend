"""Review-vote CRUD: the single choke-point for every change to ``Review.helpful_count``.

``cast_vote`` / ``remove_vote`` mutate the ``review_votes`` row **and** the
denormalized ``Review.helpful_count`` together, without committing — the caller
owns the transaction (same contract as ``crud_inventory.apply_delta`` and
``crud_review.set_admin_response``). Keeping the count in step here, rather than
aggregating on read, is the project discipline: one authoritative place to change
it, so the tally can never drift from the rows. ``get_user_votes`` is the read
helper that tells a product page which reviews the caller has already voted on.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.review import Review
from app.models.review_vote import ReviewVote


class CRUDReviewVote:
    """Helpful-vote choke-point (no commit; caller owns the txn)."""

    def get(self, db: Session, *, review_id: int, user_id: int) -> ReviewVote | None:
        """The caller's vote row for a review, or None."""
        return (
            db.query(ReviewVote)
            .filter(ReviewVote.review_id == review_id, ReviewVote.user_id == user_id)
            .first()
        )

    def cast_vote(self, db: Session, *, review: Review, user_id: int) -> ReviewVote:
        """Record ``user_id``'s "helpful" vote on ``review`` (idempotent).

        If the vote already exists this is a no-op — the existing row is returned
        and the count is left alone, so re-voting (a double-click, a retry) can
        never inflate ``helpful_count``. Otherwise a new row is staged and
        ``review.helpful_count`` is bumped by one. The ``uq_review_votes_review_user``
        constraint is the DB-level backstop for a genuine concurrent double-insert.

        Does not commit — the caller owns the transaction boundary.
        """
        existing = self.get(db, review_id=review.id, user_id=user_id)
        if existing is not None:
            return existing
        vote = ReviewVote(review_id=review.id, user_id=user_id)
        db.add(vote)
        review.helpful_count = (review.helpful_count or 0) + 1
        db.add(review)
        return vote

    def remove_vote(self, db: Session, *, review: Review, user_id: int) -> bool:
        """Withdraw ``user_id``'s vote on ``review`` if present.

        Returns whether a row was removed; when it was, ``helpful_count`` is
        decremented (clamped at 0 defensively so a drifted count can never go
        negative). Idempotent: un-voting when there is no vote is a no-op.

        Does not commit — the caller owns the transaction boundary.
        """
        existing = self.get(db, review_id=review.id, user_id=user_id)
        if existing is None:
            return False
        db.delete(existing)
        review.helpful_count = max(0, (review.helpful_count or 0) - 1)
        db.add(review)
        return True

    def voted_review_ids_for_product(
        self, db: Session, *, user_id: int, product_id: int
    ) -> set[int]:
        """The ids of ``product_id``'s reviews that ``user_id`` has voted "helpful" on.

        One join query covering every review of the product (not just a page), so
        the storefront can light up the pressed state across the whole list from a
        single ``GET /reviews/my-votes`` call. A vote on a since-unapproved review
        is returned harmlessly — the frontend only renders pressed state for
        reviews it is actually displaying.
        """
        rows = (
            db.query(ReviewVote.review_id)
            .join(Review, ReviewVote.review_id == Review.id)
            .filter(ReviewVote.user_id == user_id, Review.product_id == product_id)
            .all()
        )
        return {row[0] for row in rows}


review_vote = CRUDReviewVote()
