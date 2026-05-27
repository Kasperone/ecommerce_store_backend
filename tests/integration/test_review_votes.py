"""Integration tests for the review helpful-vote routes.

Nested under ``/api/v1/products/{id}/reviews``: PUT/DELETE ``/{rid}/vote`` (authed,
approved-review-only, self-vote forbidden, idempotent) and GET ``/my-votes``
(authed). Also covers ``helpful_count`` surfacing on the public list and the
``sort_by=helpful`` ordering.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.review import Review, ReviewStatus

pytestmark = pytest.mark.integration

GOOD_BODY = "Genuinely impressed with the quality and finish."


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


async def _post_approved(
    client, db: Session, user, product, *, rating: int = 5, body: str = GOOD_BODY
) -> int:
    """POST a review as ``user`` then flip it to approved; return its id."""
    resp = await client.post(
        f"/api/v1/products/{product.id}/reviews",
        json={"rating": rating, "body": body},
        headers=_headers(user),
    )
    assert resp.status_code == 201
    review_id = resp.json()["id"]
    row = db.query(Review).filter(Review.id == review_id).first()
    assert row is not None
    row.status = ReviewStatus.APPROVED.value
    db.commit()
    return review_id


# ---------------------------------------------------------------------------
# vote / un-vote
# ---------------------------------------------------------------------------


async def test_vote_requires_auth(client, user_factory, product_factory, db_session):
    author = user_factory()
    product = product_factory()
    rid = await _post_approved(client, db_session, author, product)
    resp = await client.put(f"/api/v1/products/{product.id}/reviews/{rid}/vote")
    assert resp.status_code == 401


async def test_vote_increments_and_sets_my_vote(client, user_factory, product_factory, db_session):
    author = user_factory()
    voter = user_factory()
    product = product_factory()
    rid = await _post_approved(client, db_session, author, product)
    resp = await client.put(
        f"/api/v1/products/{product.id}/reviews/{rid}/vote", headers=_headers(voter)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_id"] == rid
    assert body["helpful_count"] == 1
    assert body["my_vote"] is True


async def test_vote_is_idempotent(client, user_factory, product_factory, db_session):
    author = user_factory()
    voter = user_factory()
    product = product_factory()
    rid = await _post_approved(client, db_session, author, product)
    await client.put(f"/api/v1/products/{product.id}/reviews/{rid}/vote", headers=_headers(voter))
    second = await client.put(
        f"/api/v1/products/{product.id}/reviews/{rid}/vote", headers=_headers(voter)
    )
    assert second.status_code == 200
    assert second.json()["helpful_count"] == 1  # re-voting does not double-count


async def test_unvote_decrements_and_clears_my_vote(
    client, user_factory, product_factory, db_session
):
    author = user_factory()
    voter = user_factory()
    product = product_factory()
    rid = await _post_approved(client, db_session, author, product)
    await client.put(f"/api/v1/products/{product.id}/reviews/{rid}/vote", headers=_headers(voter))
    resp = await client.delete(
        f"/api/v1/products/{product.id}/reviews/{rid}/vote", headers=_headers(voter)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["helpful_count"] == 0
    assert body["my_vote"] is False


async def test_unvote_requires_auth(client, user_factory, product_factory, db_session):
    author = user_factory()
    product = product_factory()
    rid = await _post_approved(client, db_session, author, product)
    resp = await client.delete(f"/api/v1/products/{product.id}/reviews/{rid}/vote")
    assert resp.status_code == 401


async def test_vote_on_pending_review_404(client, user_factory, product_factory):
    author = user_factory()
    voter = user_factory()
    product = product_factory()
    posted = await client.post(
        f"/api/v1/products/{product.id}/reviews",
        json={"rating": 5, "body": GOOD_BODY},
        headers=_headers(author),
    )
    rid = posted.json()["id"]  # left pending — not publicly votable
    resp = await client.put(
        f"/api/v1/products/{product.id}/reviews/{rid}/vote", headers=_headers(voter)
    )
    assert resp.status_code == 404


async def test_vote_on_unknown_review_404(client, user_factory, product_factory):
    voter = user_factory()
    product = product_factory()
    resp = await client.put(
        f"/api/v1/products/{product.id}/reviews/999999/vote", headers=_headers(voter)
    )
    assert resp.status_code == 404


async def test_vote_review_under_wrong_product_404(
    client, user_factory, product_factory, db_session
):
    author = user_factory()
    voter = user_factory()
    product = product_factory()
    other = product_factory()
    rid = await _post_approved(client, db_session, author, product)
    resp = await client.put(
        f"/api/v1/products/{other.id}/reviews/{rid}/vote", headers=_headers(voter)
    )
    assert resp.status_code == 404


async def test_vote_own_review_403(client, user_factory, product_factory, db_session):
    author = user_factory()
    product = product_factory()
    rid = await _post_approved(client, db_session, author, product)
    resp = await client.put(
        f"/api/v1/products/{product.id}/reviews/{rid}/vote", headers=_headers(author)
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# my-votes
# ---------------------------------------------------------------------------


async def test_my_votes_requires_auth(client, product_factory):
    product = product_factory()
    resp = await client.get(f"/api/v1/products/{product.id}/reviews/my-votes")
    assert resp.status_code == 401


async def test_my_votes_empty_when_none(client, user_factory, product_factory):
    user = user_factory()
    product = product_factory()
    resp = await client.get(
        f"/api/v1/products/{product.id}/reviews/my-votes", headers=_headers(user)
    )
    assert resp.status_code == 200
    assert resp.json() == {"voted_review_ids": []}


async def test_my_votes_lists_voted_ids(client, user_factory, product_factory, db_session):
    a1 = user_factory()
    a2 = user_factory()
    voter = user_factory()
    product = product_factory()
    r1 = await _post_approved(client, db_session, a1, product)
    r2 = await _post_approved(client, db_session, a2, product, rating=4)
    await client.put(f"/api/v1/products/{product.id}/reviews/{r1}/vote", headers=_headers(voter))
    resp = await client.get(
        f"/api/v1/products/{product.id}/reviews/my-votes", headers=_headers(voter)
    )
    assert resp.status_code == 200
    voted = resp.json()["voted_review_ids"]
    assert voted == [r1]  # only the voted review
    assert r2 not in voted  # the un-voted review is excluded


# ---------------------------------------------------------------------------
# helpful_count + sort on the public list
# ---------------------------------------------------------------------------


async def test_public_list_carries_helpful_count(client, user_factory, product_factory, db_session):
    author = user_factory()
    voter = user_factory()
    product = product_factory()
    rid = await _post_approved(client, db_session, author, product)
    await client.put(f"/api/v1/products/{product.id}/reviews/{rid}/vote", headers=_headers(voter))
    resp = await client.get(f"/api/v1/products/{product.id}/reviews")
    assert resp.status_code == 200
    assert resp.json()["items"][0]["helpful_count"] == 1


async def test_public_list_sort_by_helpful(client, user_factory, product_factory, db_session):
    older_author = user_factory()
    newer_author = user_factory()
    voter = user_factory()
    product = product_factory()
    older = await _post_approved(client, db_session, older_author, product, rating=3)
    newer = await _post_approved(client, db_session, newer_author, product, rating=4)
    # Vote the OLDER review up so the helpful order and the newest order disagree.
    await client.put(f"/api/v1/products/{product.id}/reviews/{older}/vote", headers=_headers(voter))

    newest = await client.get(f"/api/v1/products/{product.id}/reviews")
    assert [i["id"] for i in newest.json()["items"]] == [newer, older]

    helpful = await client.get(f"/api/v1/products/{product.id}/reviews?sort_by=helpful")
    assert [i["id"] for i in helpful.json()["items"]] == [older, newer]


async def test_public_list_invalid_sort_422(client, product_factory):
    product = product_factory()
    resp = await client.get(f"/api/v1/products/{product.id}/reviews?sort_by=bogus")
    assert resp.status_code == 422
