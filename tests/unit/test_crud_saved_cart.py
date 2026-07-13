"""Unit tests for ``CRUDSavedCart`` (DB-backed, no HTTP).

Focus: the upsert state machine (create / delete-on-empty / deep-equal no-op /
change-resets-token) and the ``abandoned_candidates`` query edges (empty
items, cutoff boundary, sent latch, inactive/anonymized owner, newer-order
exclusion). The route/service layers are exercised in
``tests/integration/test_carts_recovery.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.crud import saved_cart as crud_saved_cart
from app.models.order import Order
from app.models.saved_cart import SavedCart

pytestmark = pytest.mark.integration


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _items(product_id: int = 1, quantity: int = 1) -> list[dict]:
    return [{"product_id": product_id, "variant_id": None, "quantity": quantity}]


def _make_cart(
    db: Session, user, *, items: list[dict], age_hours: float = 0, **overrides
) -> SavedCart:
    cart = SavedCart(user_id=user.id, items=items, **overrides)
    db.add(cart)
    db.commit()
    if age_hours:
        cart.updated_at = _utcnow_naive() - timedelta(hours=age_hours)
        db.add(cart)
        db.commit()
    db.refresh(cart)
    return cart


def _make_order(db: Session, user, *, order_number: str) -> Order:
    order = Order(
        user_id=user.id,
        order_number=order_number,
        subtotal=10.0,
        total=10.0,
        currency="USD",
        shipping_address="1 Main St",
        shipping_city="Warsaw",
        shipping_postal_code="00-001",
        shipping_country="PL",
    )
    db.add(order)
    db.commit()
    return order


# ---------------------------------------------------------------------------
# upsert_for_user
# ---------------------------------------------------------------------------


def test_upsert_creates_row(db_session: Session, user_factory):
    user = user_factory()
    row = crud_saved_cart.upsert_for_user(db_session, user_id=user.id, items=_items())
    db_session.commit()
    assert row is not None
    assert row.id is not None
    assert row.items == _items()


def test_upsert_empty_deletes_and_returns_none(db_session: Session, user_factory):
    user = user_factory()
    _make_cart(db_session, user, items=_items())
    assert crud_saved_cart.upsert_for_user(db_session, user_id=user.id, items=[]) is None
    db_session.commit()
    assert crud_saved_cart.get_by_user(db_session, user_id=user.id) is None


def test_upsert_empty_when_absent_is_noop(db_session: Session, user_factory):
    user = user_factory()
    assert crud_saved_cart.upsert_for_user(db_session, user_id=user.id, items=[]) is None


def test_upsert_change_resets_token_pair(db_session: Session, user_factory):
    user = user_factory()
    cart = _make_cart(
        db_session,
        user,
        items=_items(quantity=1),
        recovery_token="tok-unit-reset",
        recovery_email_sent_at=_utcnow_naive(),
    )
    crud_saved_cart.upsert_for_user(db_session, user_id=user.id, items=_items(quantity=5))
    db_session.commit()
    db_session.refresh(cart)
    assert cart.items == _items(quantity=5)
    assert cart.recovery_token is None
    assert cart.recovery_email_sent_at is None


def test_upsert_deep_equal_keeps_row_untouched(db_session: Session, user_factory):
    user = user_factory()
    cart = _make_cart(
        db_session,
        user,
        items=_items(),
        age_hours=6,
        recovery_token="tok-unit-keep",
        recovery_email_sent_at=_utcnow_naive(),
    )
    before = cart.updated_at
    result = crud_saved_cart.upsert_for_user(db_session, user_id=user.id, items=_items())
    db_session.commit()
    db_session.refresh(cart)
    assert result is cart
    assert cart.updated_at == before
    assert cart.recovery_token == "tok-unit-keep"


# ---------------------------------------------------------------------------
# get_by_token / has_newer_order
# ---------------------------------------------------------------------------


def test_get_by_token(db_session: Session, user_factory):
    user = user_factory()
    cart = _make_cart(db_session, user, items=_items(), recovery_token="tok-unit-find")
    assert crud_saved_cart.get_by_token(db_session, token="tok-unit-find") is cart
    assert crud_saved_cart.get_by_token(db_session, token="tok-unit-nope") is None


def test_has_newer_order(db_session: Session, user_factory):
    user = user_factory()
    _make_order(db_session, user, order_number="ORD-SCU-0001")
    hour_ago = _utcnow_naive() - timedelta(hours=1)
    tomorrow = _utcnow_naive() + timedelta(days=1)
    assert crud_saved_cart.has_newer_order(db_session, user_id=user.id, since=hour_ago) is True
    assert crud_saved_cart.has_newer_order(db_session, user_id=user.id, since=tomorrow) is False


# ---------------------------------------------------------------------------
# abandoned_candidates
# ---------------------------------------------------------------------------


def test_candidates_picks_only_stale_unsent_rows(db_session: Session, user_factory):
    cutoff = _utcnow_naive() - timedelta(hours=24)
    stale_user = user_factory()
    fresh_user = user_factory()
    sent_user = user_factory()
    stale = _make_cart(db_session, stale_user, items=_items(), age_hours=48)
    _make_cart(db_session, fresh_user, items=_items(), age_hours=1)
    _make_cart(
        db_session,
        sent_user,
        items=_items(),
        age_hours=48,
        recovery_token="tok-unit-sent",
        recovery_email_sent_at=_utcnow_naive(),
    )
    assert crud_saved_cart.abandoned_candidates(db_session, cutoff=cutoff) == [stale]


def test_candidates_cutoff_is_strict(db_session: Session, user_factory):
    user = user_factory()
    cart = _make_cart(db_session, user, items=_items(), age_hours=24)
    # updated_at must be strictly BEFORE the cutoff.
    assert crud_saved_cart.abandoned_candidates(db_session, cutoff=cart.updated_at) == []
    just_after = cart.updated_at + timedelta(seconds=1)
    assert crud_saved_cart.abandoned_candidates(db_session, cutoff=just_after) == [cart]


def test_candidates_excludes_empty_items(db_session: Session, user_factory):
    user = user_factory()
    _make_cart(db_session, user, items=[], age_hours=48)
    cutoff = _utcnow_naive() - timedelta(hours=24)
    assert crud_saved_cart.abandoned_candidates(db_session, cutoff=cutoff) == []


def test_candidates_excludes_inactive_and_anonymized_owner(db_session: Session, user_factory):
    inactive = user_factory(is_active=False)
    anonymized = user_factory(anonymized_at=_utcnow_naive())
    _make_cart(db_session, inactive, items=_items(), age_hours=48)
    _make_cart(db_session, anonymized, items=_items(), age_hours=48)
    cutoff = _utcnow_naive() - timedelta(hours=24)
    assert crud_saved_cart.abandoned_candidates(db_session, cutoff=cutoff) == []


def test_candidates_excludes_owner_with_newer_order(db_session: Session, user_factory):
    ordered_user = user_factory()
    quiet_user = user_factory()
    _make_cart(db_session, ordered_user, items=_items(), age_hours=48)
    quiet = _make_cart(db_session, quiet_user, items=_items(), age_hours=48)
    _make_order(db_session, ordered_user, order_number="ORD-SCU-0002")
    cutoff = _utcnow_naive() - timedelta(hours=24)
    assert crud_saved_cart.abandoned_candidates(db_session, cutoff=cutoff) == [quiet]


# ---------------------------------------------------------------------------
# delete_for_user (GDPR anonymize path)
# ---------------------------------------------------------------------------


def test_delete_for_user(db_session: Session, user_factory):
    user = user_factory()
    _make_cart(db_session, user, items=_items())
    crud_saved_cart.delete_for_user(db_session, user_id=user.id)
    db_session.commit()
    assert crud_saved_cart.get_by_user(db_session, user_id=user.id) is None
