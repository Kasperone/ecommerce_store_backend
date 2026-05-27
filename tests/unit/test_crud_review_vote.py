"""Unit tests for ``CRUDReviewVote`` (DB-backed, no HTTP).

Covers the helpful-vote choke-point: cast (idempotent, count maintenance),
withdraw (with the defensive count clamp), the per-product voted-id read helper,
and the ``uq_review_votes_review_user`` DB backstop against a duplicate row.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.crud import review_vote as crud_review_vote
from app.models.review import Review, ReviewStatus
from app.models.review_vote import ReviewVote

pytestmark = pytest.mark.integration  # needs the real DB session


def _approved(db, *, product_id: int, user_id: int, rating: int = 5) -> Review:
    """Insert an already-approved review row directly."""
    row = Review(
        product_id=product_id,
        user_id=user_id,
        rating=rating,
        body="A sufficiently long review body.",
        status=ReviewStatus.APPROVED.value,
        is_verified_purchase=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_cast_vote_creates_row_and_increments(db_session, user_factory, product_factory):
    product = product_factory()
    author = user_factory()
    voter = user_factory()
    review = _approved(db_session, product_id=product.id, user_id=author.id)

    vote = crud_review_vote.cast_vote(db_session, review=review, user_id=voter.id)
    db_session.commit()
    db_session.refresh(review)

    assert vote.id is not None
    assert review.helpful_count == 1
    assert crud_review_vote.get(db_session, review_id=review.id, user_id=voter.id) is not None


def test_cast_vote_is_idempotent(db_session, user_factory, product_factory):
    product = product_factory()
    author = user_factory()
    voter = user_factory()
    review = _approved(db_session, product_id=product.id, user_id=author.id)

    # Commit between casts to mirror the route's one-commit-per-request flow: the
    # second cast sees the first vote already persisted and no-ops. (The test
    # session is autoflush-off, so without the commit the pending first row would
    # be invisible to the second lookup — an artifact of the harness, not the code.)
    crud_review_vote.cast_vote(db_session, review=review, user_id=voter.id)
    db_session.commit()
    crud_review_vote.cast_vote(db_session, review=review, user_id=voter.id)  # re-vote = no-op
    db_session.commit()
    db_session.refresh(review)

    assert review.helpful_count == 1
    rows = db_session.query(ReviewVote).filter_by(review_id=review.id, user_id=voter.id).count()
    assert rows == 1


def test_cast_vote_counts_distinct_users(db_session, user_factory, product_factory):
    product = product_factory()
    author = user_factory()
    v1 = user_factory()
    v2 = user_factory()
    review = _approved(db_session, product_id=product.id, user_id=author.id)

    crud_review_vote.cast_vote(db_session, review=review, user_id=v1.id)
    crud_review_vote.cast_vote(db_session, review=review, user_id=v2.id)
    db_session.commit()
    db_session.refresh(review)

    assert review.helpful_count == 2


def test_remove_vote_deletes_and_decrements(db_session, user_factory, product_factory):
    product = product_factory()
    author = user_factory()
    voter = user_factory()
    review = _approved(db_session, product_id=product.id, user_id=author.id)
    crud_review_vote.cast_vote(db_session, review=review, user_id=voter.id)
    db_session.commit()

    removed = crud_review_vote.remove_vote(db_session, review=review, user_id=voter.id)
    db_session.commit()
    db_session.refresh(review)

    assert removed is True
    assert review.helpful_count == 0
    assert crud_review_vote.get(db_session, review_id=review.id, user_id=voter.id) is None


def test_remove_vote_no_op_when_absent(db_session, user_factory, product_factory):
    product = product_factory()
    author = user_factory()
    voter = user_factory()
    review = _approved(db_session, product_id=product.id, user_id=author.id)

    removed = crud_review_vote.remove_vote(db_session, review=review, user_id=voter.id)
    db_session.commit()
    db_session.refresh(review)

    assert removed is False
    assert review.helpful_count == 0


def test_remove_vote_clamps_count_at_zero(db_session, user_factory, product_factory):
    # Defensive clamp: if a vote row exists while the denormalized count has
    # somehow drifted to 0, withdrawing it must not push the count negative.
    product = product_factory()
    author = user_factory()
    voter = user_factory()
    review = _approved(db_session, product_id=product.id, user_id=author.id)
    db_session.add(ReviewVote(review_id=review.id, user_id=voter.id))
    review.helpful_count = 0
    db_session.commit()

    removed = crud_review_vote.remove_vote(db_session, review=review, user_id=voter.id)
    db_session.commit()
    db_session.refresh(review)

    assert removed is True
    assert review.helpful_count == 0


def test_voted_review_ids_for_product_scopes_correctly(db_session, user_factory, product_factory):
    product = product_factory()
    other = product_factory()
    a1 = user_factory()
    a2 = user_factory()
    a3 = user_factory()
    voter = user_factory()
    r1 = _approved(db_session, product_id=product.id, user_id=a1.id)
    r2 = _approved(db_session, product_id=product.id, user_id=a2.id)
    _approved(db_session, product_id=product.id, user_id=a3.id)  # not voted on
    other_review = _approved(db_session, product_id=other.id, user_id=a1.id)

    crud_review_vote.cast_vote(db_session, review=r1, user_id=voter.id)
    crud_review_vote.cast_vote(db_session, review=r2, user_id=voter.id)
    crud_review_vote.cast_vote(db_session, review=other_review, user_id=voter.id)
    db_session.commit()

    voted = crud_review_vote.voted_review_ids_for_product(
        db_session, user_id=voter.id, product_id=product.id
    )
    # Only this product's voted reviews — excludes the un-voted one and the other product.
    assert voted == {r1.id, r2.id}


def test_unique_constraint_blocks_duplicate_row(db_session, user_factory, product_factory):
    # The CRUD's idempotent cast prevents this in practice; the constraint is the
    # DB-level backstop against a genuine concurrent double-insert.
    product = product_factory()
    author = user_factory()
    voter = user_factory()
    review = _approved(db_session, product_id=product.id, user_id=author.id)
    db_session.add(ReviewVote(review_id=review.id, user_id=voter.id))
    db_session.flush()
    db_session.add(ReviewVote(review_id=review.id, user_id=voter.id))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()
