"""Customer + public product-review routes, nested under ``/api/v1/products``.

Public reads expose only ``approved`` reviews plus an aggregate summary. The
authenticated routes let a customer manage their single review for a product
(create / read-own / edit-own / delete-own). Editing re-queues the review for
moderation (``status`` back to ``pending``).

Admin moderation lives in ``admin_reviews.py``.
"""

from math import ceil
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_active_user, get_db
from app.crud import review as crud_review
from app.crud import review_vote as crud_review_vote
from app.models.product import Product
from app.models.review import Review, ReviewStatus
from app.models.user import User
from app.schemas.review import (
    MyVotesResponse,
    ProductReviewSummary,
    PublicReviewListResponse,
    ReviewCreate,
    ReviewResponse,
    ReviewUpdate,
    ReviewVoteResponse,
)

router = APIRouter()


def _get_product_or_404(db: Session, product_id: int, *, require_active: bool = False) -> Product:
    """Fetch a product or 404. ``require_active`` also 404s a delisted product
    (used on create so a customer can't review an inactive product, without
    leaking active-vs-inactive as a distinct status)."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if product is None or (require_active and not product.is_active):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return product


def _summary(db: Session, product_id: int) -> ProductReviewSummary:
    average, total, breakdown = crud_review.summary(db, product_id=product_id)
    return ProductReviewSummary(average_rating=average, total=total, breakdown=breakdown)


@router.get("/{product_id}/reviews", response_model=PublicReviewListResponse)
async def list_product_reviews(
    product_id: int,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort_by: Literal["newest", "helpful"] = Query(
        "newest", description="Sort by recency (newest) or helpful-vote count (helpful)"
    ),
):
    """Public: approved reviews for a product + the rating summary.

    ``sort_by`` is whitelisted to ``newest`` (default) or ``helpful``; FastAPI 422s
    anything else, so the value is never interpolated into the ordering.
    """
    _get_product_or_404(db, product_id)
    skip = (page - 1) * page_size
    items = crud_review.get_multi_public(
        db, product_id=product_id, skip=skip, limit=page_size, sort_by=sort_by
    )
    total = crud_review.count_public(db, product_id=product_id)
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": items,
        "summary": _summary(db, product_id),
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.get("/{product_id}/reviews/summary", response_model=ProductReviewSummary)
async def product_review_summary(product_id: int, db: Session = Depends(get_db)):
    """Public: aggregate rating stats for a product (approved reviews only)."""
    _get_product_or_404(db, product_id)
    return _summary(db, product_id)


@router.post(
    "/{product_id}/reviews",
    response_model=ReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_product_review(
    product_id: int,
    review_in: ReviewCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Authenticated: submit a review for a product (one per customer per product).

    Returns 404 for an unknown/inactive product, 409 if the caller already has a
    review (they should edit it instead). The new review is ``pending`` until an
    admin approves it; ``is_verified_purchase`` is snapshotted from order history.
    """
    _get_product_or_404(db, product_id, require_active=True)

    existing = crud_review.get_by_product_and_user(
        db, product_id=product_id, user_id=current_user.id
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already reviewed this product",
        )

    verified = crud_review.user_has_purchased(db, user_id=current_user.id, product_id=product_id)
    review = crud_review.create_for_user(
        db,
        product_id=product_id,
        user_id=current_user.id,
        obj_in=review_in,
        is_verified_purchase=verified,
    )
    db.commit()
    db.refresh(review)
    return review


@router.get("/{product_id}/reviews/me", response_model=ReviewResponse | None)
async def get_my_product_review(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Authenticated: the caller's own review for a product (any status).

    Returns ``200`` with ``null`` when the caller hasn't reviewed this product
    yet — "no review" is an expected state on every PDP, not an error, so the
    frontend reads it without treating a 404 as a console error.
    """
    return crud_review.get_by_product_and_user(db, product_id=product_id, user_id=current_user.id)


@router.put("/{product_id}/reviews/me", response_model=ReviewResponse)
async def update_my_product_review(
    product_id: int,
    review_in: ReviewUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Authenticated: edit the caller's own review. Re-queues it for moderation
    (``status`` back to ``pending``) since the content changed."""
    review = crud_review.get_by_product_and_user(db, product_id=product_id, user_id=current_user.id)
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")

    crud_review.update(db, db_obj=review, obj_in=review_in)
    review.status = ReviewStatus.PENDING.value
    db.commit()
    db.refresh(review)
    return review


@router.delete("/{product_id}/reviews/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_product_review(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Authenticated: delete the caller's own review for a product."""
    review = crud_review.get_by_product_and_user(db, product_id=product_id, user_id=current_user.id)
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    db.delete(review)
    db.commit()


# ----------------------------------------------------------------------
# Helpful votes
# ----------------------------------------------------------------------


def _get_approved_review_or_404(db: Session, *, product_id: int, review_id: int) -> Review:
    """Fetch an approved review under ``product_id`` or 404.

    You can only vote on a public (approved) review that actually sits under the
    product in the path. A missing, mismatched, or unapproved review all return an
    indistinguishable 404, so voting can't be used to probe moderation state.
    """
    review = (
        db.query(Review)
        .filter(
            Review.id == review_id,
            Review.product_id == product_id,
            Review.status == ReviewStatus.APPROVED.value,
        )
        .first()
    )
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    return review


@router.get("/{product_id}/reviews/my-votes", response_model=MyVotesResponse)
async def get_my_review_votes(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Authenticated: which of this product's reviews the caller voted helpful on.

    Drives the pressed state of the helpful buttons on the PDP. Client-only on the
    storefront (like ``/reviews/me``) so the SSR render stays unauthenticated.
    """
    _get_product_or_404(db, product_id)
    voted = crud_review_vote.voted_review_ids_for_product(
        db, user_id=current_user.id, product_id=product_id
    )
    return MyVotesResponse(voted_review_ids=sorted(voted))


@router.put("/{product_id}/reviews/{review_id}/vote", response_model=ReviewVoteResponse)
async def vote_review_helpful(
    product_id: int,
    review_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Authenticated: mark a review as helpful (idempotent set).

    404 if the review is missing / unapproved / not under this product; 403 on the
    caller's own review (self-votes are disallowed). Re-voting is a no-op. Returns
    the review's new helpful count + the caller's resulting vote state so the
    frontend can reconcile its optimistic toggle without a refetch.
    """
    review = _get_approved_review_or_404(db, product_id=product_id, review_id=review_id)
    if review.user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot vote on your own review",
        )
    crud_review_vote.cast_vote(db, review=review, user_id=current_user.id)
    db.commit()
    db.refresh(review)
    return ReviewVoteResponse(review_id=review.id, helpful_count=review.helpful_count, my_vote=True)


@router.delete("/{product_id}/reviews/{review_id}/vote", response_model=ReviewVoteResponse)
async def unvote_review_helpful(
    product_id: int,
    review_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Authenticated: withdraw a previous helpful vote (idempotent).

    Returns the review's new helpful count + ``my_vote=False`` (rather than a bare
    204) so the storefront can snap its displayed tally to the authoritative value,
    which may have shifted from other shoppers' concurrent votes. Un-voting when no
    vote exists is a no-op.
    """
    review = _get_approved_review_or_404(db, product_id=product_id, review_id=review_id)
    crud_review_vote.remove_vote(db, review=review, user_id=current_user.id)
    db.commit()
    db.refresh(review)
    return ReviewVoteResponse(
        review_id=review.id, helpful_count=review.helpful_count, my_vote=False
    )
