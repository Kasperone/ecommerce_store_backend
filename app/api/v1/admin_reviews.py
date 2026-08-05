"""Admin review-moderation API.

All routes are admin-gated; mutations are recorded to the audit log
(``resource_type="review"``). The list joins product + author so the moderation
table shows what's being reviewed and by whom without an N+1. Customer-facing
review submission lives in ``reviews.py``.
"""

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.capabilities import CATALOG_READ, CATALOG_WRITE
from app.core.dependencies import get_current_admin, get_db, require_capability, require_read
from app.crud import review as crud_review
from app.models.review import Review, ReviewStatus
from app.models.user import User
from app.schemas.review import (
    AdminReviewListItem,
    AdminReviewListResponse,
    ReviewModerate,
)
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

# Capability gate for this router's write/DELETE routes.
_require = Depends(require_capability(CATALOG_WRITE))
_read = Depends(require_read(CATALOG_READ))


def _to_admin_item(review: Review) -> AdminReviewListItem:
    """Build the admin list/detail item from a review with product + author loaded."""
    return AdminReviewListItem(
        id=review.id,
        created_at=review.created_at,
        updated_at=review.updated_at,
        product_id=review.product_id,
        product_name=review.product.name,
        product_slug=review.product.slug,
        user_id=review.user_id,
        author_name=review.author_name,
        author_email=review.user.email,
        rating=review.rating,
        title=review.title,
        body=review.body,
        status=ReviewStatus(review.status),
        is_verified_purchase=review.is_verified_purchase,
        helpful_count=review.helpful_count,
        admin_response=review.admin_response,
        admin_response_at=review.admin_response_at,
    )


@router.get("/", response_model=AdminReviewListResponse)
async def list_reviews(
    db: Session = Depends(get_db),
    current_admin: User = _read,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    review_status: ReviewStatus | None = Query(
        None, alias="status", description="Filter by moderation status"
    ),
    product_id: int | None = Query(None, description="Filter by product"),
    rating: int | None = Query(None, ge=1, le=5, description="Filter by star rating"),
    search: str | None = Query(
        None, max_length=100, description="Search title / body / author email / product name"
    ),
):
    """List reviews with filters (admin). Newest first."""
    skip = (page - 1) * page_size
    status_value = review_status.value if review_status is not None else None
    reviews = crud_review.get_multi_admin(
        db,
        status=status_value,
        product_id=product_id,
        rating=rating,
        search=search,
        skip=skip,
        limit=page_size,
    )
    total = crud_review.count_admin(
        db, status=status_value, product_id=product_id, rating=rating, search=search
    )
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": [_to_admin_item(r) for r in reviews],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.get("/{review_id}", response_model=AdminReviewListItem)
async def get_review(
    review_id: int,
    db: Session = Depends(get_db),
    current_admin: User = _read,
):
    """Get a single review (admin)."""
    review = crud_review.get(db, id=review_id)
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    return _to_admin_item(review)


@router.patch("/{review_id}", dependencies=[_require], response_model=AdminReviewListItem)
async def moderate_review(
    review_id: int,
    payload: ReviewModerate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Moderate a review and/or set the store's public reply (admin). Audited.

    Both fields are optional: send ``status`` to approve/reject, send
    ``admin_response`` to attach a public reply (``null`` clears it). A body with
    neither field present is a 422 — there is nothing to do.
    """
    review = crud_review.get(db, id=review_id)
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")

    fields = payload.model_fields_set
    if "status" not in fields and "admin_response" not in fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide a status and/or an admin_response",
        )

    if "status" in fields and payload.status is not None:
        review.status = payload.status
    if "admin_response" in fields:
        crud_review.set_admin_response(db, review=review, response=payload.admin_response)

    db.add(review)
    audit.record_update(resource_type="review", obj=review)
    db.commit()
    db.refresh(review)
    return _to_admin_item(review)


@router.delete("/{review_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT)
async def delete_review(
    review_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a review (admin). Audited (snapshot taken before delete)."""
    review = crud_review.get(db, id=review_id)
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")

    audit.record_delete(resource_type="review", obj=review)
    db.delete(review)
    db.commit()
