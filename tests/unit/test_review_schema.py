"""Unit tests for review schema validation (pure — no DB).

Pins the rating bounds (mirrored by the DB ``ck_reviews_rating_range`` CHECK),
the substantive-body minimum, the title cap, and that moderation can only move a
review to ``approved``/``rejected`` (never back to ``pending``).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.review import ReviewCreate, ReviewModerate, ReviewUpdate

GOOD_BODY = "Solid build quality, would buy again."


@pytest.mark.parametrize("rating", [1, 3, 5])
def test_rating_within_bounds_ok(rating):
    review = ReviewCreate(rating=rating, body=GOOD_BODY)
    assert review.rating == rating


@pytest.mark.parametrize("rating", [0, 6, -1, 100])
def test_rating_out_of_bounds_rejected(rating):
    with pytest.raises(ValidationError):
        ReviewCreate(rating=rating, body=GOOD_BODY)


def test_body_below_minimum_rejected():
    with pytest.raises(ValidationError):
        ReviewCreate(rating=5, body="too short")  # 9 chars < 10


def test_body_at_minimum_ok():
    review = ReviewCreate(rating=5, body="x" * 10)
    assert len(review.body) == 10


def test_title_optional_and_capped():
    assert ReviewCreate(rating=4, body=GOOD_BODY).title is None
    with pytest.raises(ValidationError):
        ReviewCreate(rating=4, body=GOOD_BODY, title="x" * 201)


def test_update_all_fields_optional():
    # An empty update parses — the route still re-queues the review to pending.
    assert ReviewUpdate().model_dump(exclude_unset=True) == {}


def test_update_still_validates_provided_fields():
    with pytest.raises(ValidationError):
        ReviewUpdate(rating=9)


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_moderate_accepts_terminal_states(status):
    assert ReviewModerate(status=status).status == status


@pytest.mark.parametrize("status", ["pending", "deleted", ""])
def test_moderate_rejects_non_terminal(status):
    with pytest.raises(ValidationError):
        ReviewModerate(status=status)
