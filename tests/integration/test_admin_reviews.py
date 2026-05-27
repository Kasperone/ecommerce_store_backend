"""Integration tests for the admin review-moderation API (/api/v1/admin/reviews).

Covers authz, list + filters (status/rating/product/search), detail, the
approve/reject PATCH + audit row, and delete + audit row. Plan:
``ecommerce_store/.claude/notes/plans/reviews/in-progress/2026-05-27-product-reviews.md``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.review import Review, ReviewStatus
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _audit_count(db: Session, *, action: str, resource_id: int) -> int:
    return (
        db.query(AdminAuditEvent)
        .filter(
            AdminAuditEvent.resource_type == "review",
            AdminAuditEvent.action == action,
            AdminAuditEvent.resource_id == resource_id,
        )
        .count()
    )


def _seed_review(
    db: Session,
    *,
    product_id: int,
    user_id: int,
    rating: int = 5,
    status: str = ReviewStatus.PENDING.value,
    body: str = "A perfectly reasonable review body.",
) -> Review:
    row = Review(
        product_id=product_id,
        user_id=user_id,
        rating=rating,
        body=body,
        status=status,
        is_verified_purchase=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_list_requires_admin(client, user_factory):
    customer = user_factory(role=UserRole.CUSTOMER)
    assert (await client.get("/api/v1/admin/reviews/")).status_code == 401
    assert (
        await client.get("/api/v1/admin/reviews/", headers=_headers(customer))
    ).status_code == 403


# ---------------------------------------------------------------------------
# list + filters
# ---------------------------------------------------------------------------


async def test_list_returns_product_and_author(client, user_factory, product_factory, db_session):
    admin = user_factory(role=UserRole.ADMIN)
    reviewer = user_factory(email="rev@example.com", first_name="Ann", last_name="Smith")
    product = product_factory(name="Test Phone")
    _seed_review(db_session, product_id=product.id, user_id=reviewer.id)

    resp = await client.get("/api/v1/admin/reviews/", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["product_name"] == "Test Phone"
    assert row["author_email"] == "rev@example.com"
    assert row["author_name"] == "Ann S."
    assert row["status"] == "pending"


async def test_list_filters(client, user_factory, product_factory, db_session):
    admin = user_factory(role=UserRole.ADMIN)
    u1 = user_factory(email="u1@example.com")
    u2 = user_factory(email="u2@example.com")
    p1 = product_factory(name="Alpha")
    p2 = product_factory(name="Beta")
    _seed_review(
        db_session, product_id=p1.id, user_id=u1.id, rating=5, status=ReviewStatus.APPROVED.value
    )
    _seed_review(
        db_session, product_id=p2.id, user_id=u2.id, rating=2, status=ReviewStatus.PENDING.value
    )

    async def total(qs: str) -> int:
        resp = await client.get(f"/api/v1/admin/reviews/{qs}", headers=_headers(admin))
        assert resp.status_code == 200
        return resp.json()["total"]

    assert await total("") == 2
    assert await total("?status=approved") == 1
    assert await total("?status=pending") == 1
    assert await total("?rating=5") == 1
    assert await total(f"?product_id={p2.id}") == 1
    assert await total("?search=Alpha") == 1


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


async def test_detail_and_404(client, user_factory, product_factory, db_session):
    admin = user_factory(role=UserRole.ADMIN)
    reviewer = user_factory()
    product = product_factory()
    review = _seed_review(db_session, product_id=product.id, user_id=reviewer.id)

    ok = await client.get(f"/api/v1/admin/reviews/{review.id}", headers=_headers(admin))
    assert ok.status_code == 200
    assert ok.json()["id"] == review.id

    missing = await client.get("/api/v1/admin/reviews/999999", headers=_headers(admin))
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# moderate (approve / reject) + audit
# ---------------------------------------------------------------------------


async def test_moderate_approve_writes_audit(client, user_factory, product_factory, db_session):
    admin = user_factory(role=UserRole.ADMIN)
    reviewer = user_factory()
    product = product_factory()
    review = _seed_review(db_session, product_id=product.id, user_id=reviewer.id)

    resp = await client.patch(
        f"/api/v1/admin/reviews/{review.id}",
        json={"status": "approved"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    db_session.refresh(review)
    assert review.status == ReviewStatus.APPROVED.value
    assert _audit_count(db_session, action="update", resource_id=review.id) == 1


async def test_moderate_reject(client, user_factory, product_factory, db_session):
    admin = user_factory(role=UserRole.ADMIN)
    reviewer = user_factory()
    product = product_factory()
    review = _seed_review(db_session, product_id=product.id, user_id=reviewer.id)

    resp = await client.patch(
        f"/api/v1/admin/reviews/{review.id}",
        json={"status": "rejected"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


async def test_moderate_invalid_status_422(client, user_factory, product_factory, db_session):
    admin = user_factory(role=UserRole.ADMIN)
    reviewer = user_factory()
    product = product_factory()
    review = _seed_review(db_session, product_id=product.id, user_id=reviewer.id)

    resp = await client.patch(
        f"/api/v1/admin/reviews/{review.id}",
        json={"status": "pending"},  # not a terminal moderation state
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_moderate_404(client, user_factory):
    admin = user_factory(role=UserRole.ADMIN)
    resp = await client.patch(
        "/api/v1/admin/reviews/999999",
        json={"status": "approved"},
        headers=_headers(admin),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# admin reply (set / clear / combined) + audit
# ---------------------------------------------------------------------------


async def test_set_admin_response_stamps_and_audits(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(role=UserRole.ADMIN)
    reviewer = user_factory()
    product = product_factory()
    review = _seed_review(db_session, product_id=product.id, user_id=reviewer.id)

    resp = await client.patch(
        f"/api/v1/admin/reviews/{review.id}",
        json={"admin_response": "Thanks for your feedback!"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["admin_response"] == "Thanks for your feedback!"
    assert body["admin_response_at"] is not None
    assert body["status"] == "pending"  # a reply-only PATCH leaves status untouched
    assert _audit_count(db_session, action="update", resource_id=review.id) == 1


async def test_clear_admin_response(client, user_factory, product_factory, db_session):
    admin = user_factory(role=UserRole.ADMIN)
    reviewer = user_factory()
    product = product_factory()
    review = _seed_review(db_session, product_id=product.id, user_id=reviewer.id)

    await client.patch(
        f"/api/v1/admin/reviews/{review.id}",
        json={"admin_response": "Initial reply"},
        headers=_headers(admin),
    )
    cleared = await client.patch(
        f"/api/v1/admin/reviews/{review.id}",
        json={"admin_response": None},
        headers=_headers(admin),
    )
    assert cleared.status_code == 200
    assert cleared.json()["admin_response"] is None
    assert cleared.json()["admin_response_at"] is None


async def test_moderate_status_and_reply_together(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(role=UserRole.ADMIN)
    reviewer = user_factory()
    product = product_factory()
    review = _seed_review(db_session, product_id=product.id, user_id=reviewer.id)

    resp = await client.patch(
        f"/api/v1/admin/reviews/{review.id}",
        json={"status": "approved", "admin_response": "Glad you like it!"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["admin_response"] == "Glad you like it!"


async def test_moderate_empty_body_422(client, user_factory, product_factory, db_session):
    admin = user_factory(role=UserRole.ADMIN)
    reviewer = user_factory()
    product = product_factory()
    review = _seed_review(db_session, product_id=product.id, user_id=reviewer.id)

    resp = await client.patch(
        f"/api/v1/admin/reviews/{review.id}",
        json={},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


async def test_moderate_reply_over_max_422(client, user_factory, product_factory, db_session):
    admin = user_factory(role=UserRole.ADMIN)
    reviewer = user_factory()
    product = product_factory()
    review = _seed_review(db_session, product_id=product.id, user_id=reviewer.id)

    resp = await client.patch(
        f"/api/v1/admin/reviews/{review.id}",
        json={"admin_response": "x" * 2001},
        headers=_headers(admin),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# delete + audit
# ---------------------------------------------------------------------------


async def test_delete_writes_audit_and_removes(client, user_factory, product_factory, db_session):
    admin = user_factory(role=UserRole.ADMIN)
    reviewer = user_factory()
    product = product_factory()
    review = _seed_review(db_session, product_id=product.id, user_id=reviewer.id)
    review_id = review.id

    resp = await client.delete(f"/api/v1/admin/reviews/{review_id}", headers=_headers(admin))
    assert resp.status_code == 204
    assert db_session.get(Review, review_id) is None
    assert _audit_count(db_session, action="delete", resource_id=review_id) == 1


async def test_delete_404(client, user_factory):
    admin = user_factory(role=UserRole.ADMIN)
    resp = await client.delete("/api/v1/admin/reviews/999999", headers=_headers(admin))
    assert resp.status_code == 404
