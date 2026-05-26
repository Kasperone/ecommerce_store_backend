"""Unit tests for ``CRUDReview`` (DB-backed, no HTTP).

Covers create + dup lookup, the approved-only summary math, the
verified-purchase detection (non-cancelled order containing the product), and
the admin filter query.
"""

from __future__ import annotations

import pytest

from app.crud import review as crud_review
from app.models.order import Order, OrderItem, OrderStatus
from app.models.review import Review, ReviewStatus
from app.schemas.review import ReviewCreate

pytestmark = pytest.mark.integration  # needs the real DB session


def _approved(db, *, product_id: int, user_id: int, rating: int) -> Review:
    """Insert an already-approved review row directly (bypasses the pending flow)."""
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


def _place_order(db, *, user_id: int, product_id: int, status: OrderStatus) -> None:
    order = Order(
        user_id=user_id,
        order_number=f"ORD-{user_id}-{product_id}-{status.value}",
        status=status,
        subtotal=10.0,
        total=10.0,
        currency="USD",
        shipping_address="1 Test St",
        shipping_city="Testville",
        shipping_postal_code="00-001",
        shipping_country="Poland",
    )
    db.add(order)
    db.flush()
    db.add(
        OrderItem(
            order_id=order.id,
            product_id=product_id,
            product_name="snapshot",
            quantity=1,
            unit_price=10.0,
            total_price=10.0,
        )
    )
    db.commit()


def test_create_for_user_is_pending_with_id(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory()
    review = crud_review.create_for_user(
        db_session,
        product_id=product.id,
        user_id=user.id,
        obj_in=ReviewCreate(rating=5, body="Great, highly recommend it."),
        is_verified_purchase=True,
    )
    assert review.id is not None
    assert review.status == ReviewStatus.PENDING.value
    assert review.is_verified_purchase is True


def test_get_by_product_and_user(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory()
    before = crud_review.get_by_product_and_user(db_session, product_id=product.id, user_id=user.id)
    assert before is None
    crud_review.create_for_user(
        db_session,
        product_id=product.id,
        user_id=user.id,
        obj_in=ReviewCreate(rating=4, body="Pretty good overall."),
        is_verified_purchase=False,
    )
    db_session.commit()
    found = crud_review.get_by_product_and_user(db_session, product_id=product.id, user_id=user.id)
    assert found is not None
    assert found.rating == 4


def test_summary_counts_only_approved(db_session, user_factory, product_factory):
    product = product_factory()
    u1 = user_factory()
    u2 = user_factory()
    u3 = user_factory()
    _approved(db_session, product_id=product.id, user_id=u1.id, rating=5)
    _approved(db_session, product_id=product.id, user_id=u2.id, rating=3)
    # A pending review for the same product must not count toward the summary.
    crud_review.create_for_user(
        db_session,
        product_id=product.id,
        user_id=u3.id,
        obj_in=ReviewCreate(rating=1, body="Pending, should be excluded."),
        is_verified_purchase=False,
    )
    db_session.commit()

    average, total, breakdown = crud_review.summary(db_session, product_id=product.id)
    assert total == 2
    assert average == 4.0  # (5 + 3) / 2
    assert breakdown["5"] == 1
    assert breakdown["3"] == 1
    assert breakdown["1"] == 0  # the pending 1-star is excluded


def test_summary_empty_product(db_session, product_factory):
    product = product_factory()
    average, total, breakdown = crud_review.summary(db_session, product_id=product.id)
    assert total == 0
    assert average == 0.0
    assert set(breakdown.keys()) == {"1", "2", "3", "4", "5"}
    assert all(v == 0 for v in breakdown.values())


def test_user_has_purchased_true_for_non_cancelled(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory()
    _place_order(db_session, user_id=user.id, product_id=product.id, status=OrderStatus.PAID)
    purchased = crud_review.user_has_purchased(db_session, user_id=user.id, product_id=product.id)
    assert purchased is True


def test_user_has_purchased_false_when_only_cancelled(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory()
    _place_order(db_session, user_id=user.id, product_id=product.id, status=OrderStatus.CANCELLED)
    purchased = crud_review.user_has_purchased(db_session, user_id=user.id, product_id=product.id)
    assert purchased is False


def test_user_has_purchased_false_without_order(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory()
    purchased = crud_review.user_has_purchased(db_session, user_id=user.id, product_id=product.id)
    assert purchased is False


def test_admin_query_filters(db_session, user_factory, product_factory):
    product_a = product_factory(name="Alpha Widget")
    product_b = product_factory(name="Beta Gadget")
    u1 = user_factory(email="reviewer-a@example.com")
    u2 = user_factory(email="reviewer-b@example.com")
    _approved(db_session, product_id=product_a.id, user_id=u1.id, rating=5)
    pending = crud_review.create_for_user(
        db_session,
        product_id=product_b.id,
        user_id=u2.id,
        obj_in=ReviewCreate(rating=2, body="Did not meet expectations."),
        is_verified_purchase=False,
    )
    db_session.commit()

    assert crud_review.count_admin(db_session) == 2
    assert crud_review.count_admin(db_session, status="pending") == 1
    assert crud_review.count_admin(db_session, rating=5) == 1
    assert crud_review.count_admin(db_session, product_id=product_b.id) == 1
    # search hits product name, author email, and body
    assert crud_review.count_admin(db_session, search="Alpha") == 1
    assert crud_review.count_admin(db_session, search="reviewer-b") == 1
    assert crud_review.count_admin(db_session, search="expectations") == 1

    pending_rows = crud_review.get_multi_admin(db_session, status="pending")
    assert [r.id for r in pending_rows] == [pending.id]
