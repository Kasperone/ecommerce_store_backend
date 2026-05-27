"""Review schemas.

Customer-facing schemas power the storefront review widget and submission flow
nested under ``/api/v1/products/{id}/reviews``. Admin schemas power the
moderation view at ``/api/v1/admin/reviews``.

``body`` requires >= 10 chars so a review is substantive enough to moderate;
``rating`` is bounded 1-5 (mirrored by the DB ``ck_reviews_rating_range`` CHECK).
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from app.models.review import ReviewStatus
from app.schemas.base import BaseSchema, TimestampSchema

BODY_MIN = 10
BODY_MAX = 5000
TITLE_MAX = 200
ADMIN_RESPONSE_MAX = 2000


class ReviewCreate(BaseSchema):
    """Customer POST body — create a review for a product."""

    rating: Annotated[int, Field(ge=1, le=5)]
    title: Annotated[str | None, Field(max_length=TITLE_MAX)] = None
    body: Annotated[str, Field(min_length=BODY_MIN, max_length=BODY_MAX)]


class ReviewUpdate(BaseSchema):
    """Customer PUT body — edit own review (re-queues it for moderation)."""

    rating: Annotated[int | None, Field(ge=1, le=5)] = None
    title: Annotated[str | None, Field(max_length=TITLE_MAX)] = None
    body: Annotated[str | None, Field(min_length=BODY_MIN, max_length=BODY_MAX)] = None


class ReviewResponse(TimestampSchema):
    """A single review as returned to customers (public list + own review)."""

    product_id: int
    user_id: int
    rating: int
    title: str | None = None
    body: str
    status: ReviewStatus
    is_verified_purchase: bool
    author_name: str
    helpful_count: int
    admin_response: str | None = None
    admin_response_at: datetime | None = None


class ReviewVoteResponse(BaseSchema):
    """Returned after casting/withdrawing a helpful vote.

    Carries the review's new ``helpful_count`` plus the caller's resulting state
    so the frontend can reconcile its optimistic toggle without a refetch.
    """

    review_id: int
    helpful_count: int
    my_vote: bool


class MyVotesResponse(BaseSchema):
    """The reviews the caller has voted "helpful" on for a given product.

    Drives the pressed state of the helpful button across a PDP's review list.
    Client-only on the storefront (mirrors ``GET /reviews/me``) so SSR stays
    unauthenticated.
    """

    voted_review_ids: list[int]


class ProductReviewSummary(BaseSchema):
    """Aggregate rating stats for a product (approved reviews only)."""

    average_rating: float  # rounded to 1 decimal; 0.0 when no approved reviews
    total: int
    breakdown: dict[str, int]  # star -> count, e.g. {"1": 0, ..., "5": 12}


class PublicReviewListResponse(BaseSchema):
    """Paginated public review list bundled with the product's summary."""

    items: list[ReviewResponse]
    summary: ProductReviewSummary
    total: int
    page: int
    page_size: int
    pages: int


# --- Admin moderation ---


class AdminReviewListItem(TimestampSchema):
    """A review row for the admin moderation list (joins product + author)."""

    product_id: int
    product_name: str
    product_slug: str
    user_id: int
    author_name: str
    author_email: str
    rating: int
    title: str | None = None
    body: str
    status: ReviewStatus
    is_verified_purchase: bool
    helpful_count: int
    admin_response: str | None = None
    admin_response_at: datetime | None = None


class ReviewModerate(BaseSchema):
    """Admin PATCH body — approve/reject and/or set/clear the store's reply.

    Both fields are optional and independent: send ``status`` to moderate, send
    ``admin_response`` to attach a public reply (``null`` clears it, and the
    server stamps/un-stamps ``admin_response_at``). The route 422s a body with
    neither field set. ``pending`` is intentionally not an accepted status:
    moderation is a forward decision, not a way to un-decide.
    """

    status: Literal["approved", "rejected"] | None = None
    admin_response: Annotated[str | None, Field(max_length=ADMIN_RESPONSE_MAX)] = None


class AdminReviewListResponse(BaseSchema):
    """Paginated admin review list."""

    items: list[AdminReviewListItem]
    total: int
    page: int
    page_size: int
    pages: int
