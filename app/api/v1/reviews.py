"""Customer + public product-review routes, nested under ``/api/v1/products``.

Public reads expose only ``approved`` reviews plus an aggregate summary. The
authenticated routes let a customer manage their single review for a product
(create / read-own / edit-own / delete-own). Editing re-queues the review for
moderation (``status`` back to ``pending``).

Admin moderation lives in ``admin_reviews.py``.
"""

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_active_user, get_db
from app.crud import review as crud_review
from app.models.product import Product
from app.models.review import ReviewStatus
from app.models.user import User
from app.schemas.review import (
    ProductReviewSummary,
    PublicReviewListResponse,
    ReviewCreate,
    ReviewResponse,
    ReviewUpdate,
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
):
    """Public: approved reviews for a product (newest first) + the rating summary."""
    _get_product_or_404(db, product_id)
    skip = (page - 1) * page_size
    items = crud_review.get_multi_public(db, product_id=product_id, skip=skip, limit=page_size)
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
