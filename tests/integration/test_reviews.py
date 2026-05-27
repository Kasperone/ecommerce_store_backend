"""Integration tests for customer + public review routes.

Nested under ``/api/v1/products/{id}/reviews``: public approved-only list +
summary, authed create (one per customer, verified-purchase snapshot, 409 dup,
404 unknown/inactive product, 422 validation), and own-review get/edit/delete
(editing re-queues to pending). Plan:
``ecommerce_store/.claude/notes/plans/reviews/in-progress/2026-05-27-product-reviews.md``.
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


def _approve(db: Session, *, product_id: int, user_id: int) -> None:
    """Flip a customer's review to approved so it surfaces publicly."""
    row = (
        db.query(Review).filter(Review.product_id == product_id, Review.user_id == user_id).first()
    )
    assert row is not None
    row.status = ReviewStatus.APPROVED.value
    db.commit()


def _order_body(product_id: int) -> dict:
    return {
        "items": [{"product_id": product_id, "quantity": 1}],
        "currency": "USD",
        "shipping_address": "1 Test St",
        "shipping_city": "Testville",
        "shipping_postal_code": "00-001",
        "shipping_country": "Poland",
    }


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_requires_auth(client, product_factory):
    product = product_factory()
    resp = await client.post(
        f"/api/v1/products/{product.id}/reviews", json={"rating": 5, "body": GOOD_BODY}
    )
    assert resp.status_code == 401


async def test_create_review_is_pending_and_unverified(client, user_factory, product_factory):
    user = user_factory()
    product = product_factory()
    resp = await client.post(
        f"/api/v1/products/{product.id}/reviews",
        json={"rating": 5, "title": "Love it", "body": GOOD_BODY},
        headers=_headers(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["is_verified_purchase"] is False
    assert body["rating"] == 5
    assert body["author_name"]  # privacy-safe display name present


async def test_create_duplicate_returns_409(client, user_factory, product_factory):
    user = user_factory()
    product = product_factory()
    first = await client.post(
        f"/api/v1/products/{product.id}/reviews",
        json={"rating": 4, "body": GOOD_BODY},
        headers=_headers(user),
    )
    assert first.status_code == 201
    dup = await client.post(
        f"/api/v1/products/{product.id}/reviews",
        json={"rating": 3, "body": "Changed my mind a little here."},
        headers=_headers(user),
    )
    assert dup.status_code == 409


async def test_create_unknown_product_404(client, user_factory):
    user = user_factory()
    resp = await client.post(
        "/api/v1/products/999999/reviews",
        json={"rating": 5, "body": GOOD_BODY},
        headers=_headers(user),
    )
    assert resp.status_code == 404


async def test_create_inactive_product_404(client, user_factory, product_factory):
    user = user_factory()
    product = product_factory(is_active=False)
    resp = await client.post(
        f"/api/v1/products/{product.id}/reviews",
        json={"rating": 5, "body": GOOD_BODY},
        headers=_headers(user),
    )
    assert resp.status_code == 404


async def test_create_validation_422(client, user_factory, product_factory):
    user = user_factory()
    product = product_factory()
    bad_rating = await client.post(
        f"/api/v1/products/{product.id}/reviews",
        json={"rating": 6, "body": GOOD_BODY},
        headers=_headers(user),
    )
    assert bad_rating.status_code == 422
    short_body = await client.post(
        f"/api/v1/products/{product.id}/reviews",
        json={"rating": 5, "body": "short"},
        headers=_headers(user),
    )
    assert short_body.status_code == 422


async def test_create_sets_verified_purchase_after_order(client, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=25.0)
    order = await client.post(
        "/api/v1/orders/", json=_order_body(product.id), headers=_headers(user)
    )
    assert order.status_code == 201

    resp = await client.post(
        f"/api/v1/products/{product.id}/reviews",
        json={"rating": 5, "body": GOOD_BODY},
        headers=_headers(user),
    )
    assert resp.status_code == 201
    assert resp.json()["is_verified_purchase"] is True


# ---------------------------------------------------------------------------
# public list + summary
# ---------------------------------------------------------------------------


async def test_public_list_shows_only_approved(client, user_factory, product_factory, db_session):
    product = product_factory()
    approver = user_factory()
    pender = user_factory()

    await client.post(
        f"/api/v1/products/{product.id}/reviews",
        json={"rating": 5, "body": GOOD_BODY},
        headers=_headers(approver),
    )
    await client.post(
        f"/api/v1/products/{product.id}/reviews",
        json={"rating": 1, "body": "This one stays pending for now."},
        headers=_headers(pender),
    )
    _approve(db_session, product_id=product.id, user_id=approver.id)

    resp = await client.get(f"/api/v1/products/{product.id}/reviews")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["rating"] == 5
    assert body["summary"]["total"] == 1
    assert body["summary"]["average_rating"] == 5.0
    assert body["summary"]["breakdown"]["5"] == 1


async def test_public_list_unknown_product_404(client):
    assert (await client.get("/api/v1/products/999999/reviews")).status_code == 404


async def test_summary_endpoint(client, user_factory, product_factory, db_session):
    product = product_factory()
    user = user_factory()
    await client.post(
        f"/api/v1/products/{product.id}/reviews",
        json={"rating": 4, "body": GOOD_BODY},
        headers=_headers(user),
    )
    _approve(db_session, product_id=product.id, user_id=user.id)

    resp = await client.get(f"/api/v1/products/{product.id}/reviews/summary")
    assert resp.status_code == 200
    assert resp.json()["average_rating"] == 4.0
    assert resp.json()["total"] == 1


# ---------------------------------------------------------------------------
# own review get / edit / delete
# ---------------------------------------------------------------------------


async def test_get_my_review_404_when_none(client, user_factory, product_factory):
    user = user_factory()
    product = product_factory()
    resp = await client.get(f"/api/v1/products/{product.id}/reviews/me", headers=_headers(user))
    assert resp.status_code == 404


async def test_edit_my_review_resets_to_pending(client, user_factory, product_factory, db_session):
    user = user_factory()
    product = product_factory()
    await client.post(
        f"/api/v1/products/{product.id}/reviews",
        json={"rating": 5, "body": GOOD_BODY},
        headers=_headers(user),
    )
    _approve(db_session, product_id=product.id, user_id=user.id)

    resp = await client.put(
        f"/api/v1/products/{product.id}/reviews/me",
        json={"rating": 2, "body": "Revised after a month of use, less happy."},
        headers=_headers(user),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rating"] == 2
    assert body["status"] == "pending"  # editing re-queues for moderation


async def test_delete_my_review(client, user_factory, product_factory):
    user = user_factory()
    product = product_factory()
    await client.post(
        f"/api/v1/products/{product.id}/reviews",
        json={"rating": 5, "body": GOOD_BODY},
        headers=_headers(user),
    )
    deleted = await client.delete(
        f"/api/v1/products/{product.id}/reviews/me", headers=_headers(user)
    )
    assert deleted.status_code == 204
    gone = await client.get(f"/api/v1/products/{product.id}/reviews/me", headers=_headers(user))
    assert gone.status_code == 404
