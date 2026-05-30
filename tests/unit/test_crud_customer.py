"""Unit tests for the admin-customers CRUD methods on CRUDUser (DB-backed)."""

from __future__ import annotations

import pytest

from app.crud import order as crud_order
from app.crud import user as crud_user
from app.models.user import UserRole
from app.schemas.order import OrderCreate, OrderItemCreate

pytestmark = pytest.mark.integration


def _place_order(db, user, product, *, shipping_method_id: int, qty=1, currency="USD"):
    return crud_order.create_with_items(
        db,
        user_id=user.id,
        obj_in=OrderCreate(
            items=[OrderItemCreate(product_id=product.id, quantity=qty)],
            currency=currency,
            shipping_address="1 St",
            shipping_city="Town",
            shipping_postal_code="00-001",
            shipping_country="PL",
            shipping_method_id=shipping_method_id,
        ),
    )


def test_get_multi_admin_filters_by_role(db_session, user_factory):
    user_factory(email="c1@example.com", role=UserRole.CUSTOMER)
    user_factory(email="a1@example.com", role=UserRole.ADMIN)

    customers = crud_user.get_multi_admin(db_session, role=UserRole.CUSTOMER)
    assert all(u.role == UserRole.CUSTOMER for u in customers)
    assert "a1@example.com" not in [u.email for u in customers]


def test_get_multi_admin_filters_active_and_verified(db_session, user_factory):
    user_factory(email="active-v@example.com", is_active=True, is_verified=True)
    user_factory(email="inactive@example.com", is_active=False, is_verified=True)
    user_factory(email="unverified@example.com", is_active=True, is_verified=False)

    inactive = crud_user.get_multi_admin(db_session, is_active=False)
    assert [u.email for u in inactive] == ["inactive@example.com"]

    unverified = crud_user.get_multi_admin(db_session, is_verified=False)
    assert [u.email for u in unverified] == ["unverified@example.com"]


def test_get_multi_admin_search(db_session, user_factory):
    user_factory(email="alice@example.com", first_name="Alice", last_name="Wong")
    user_factory(email="bob@example.com", first_name="Bob", last_name="Stone")

    by_email = crud_user.get_multi_admin(db_session, search="alice")
    assert [u.email for u in by_email] == ["alice@example.com"]

    by_last = crud_user.get_multi_admin(db_session, search="stone")
    assert [u.email for u in by_last] == ["bob@example.com"]


def test_count_admin_matches_filter(db_session, user_factory):
    user_factory(email="x1@example.com", role=UserRole.CUSTOMER)
    user_factory(email="x2@example.com", role=UserRole.CUSTOMER)
    user_factory(email="x3@example.com", role=UserRole.ADMIN)

    assert crud_user.count_admin(db_session, role=UserRole.CUSTOMER) == 2
    assert crud_user.count_admin(db_session) == 3


def test_order_counts_for(db_session, user_factory, product_factory, test_shipping_method_id):
    u1 = user_factory(email="o1@example.com")
    u2 = user_factory(email="o2@example.com")
    product = product_factory(stock=50, price_usd=10.0)
    _place_order(db_session, u1, product, shipping_method_id=test_shipping_method_id)
    _place_order(db_session, u1, product, shipping_method_id=test_shipping_method_id)
    _place_order(db_session, u2, product, shipping_method_id=test_shipping_method_id)
    db_session.flush()

    counts = crud_user.order_counts_for(db_session, user_ids=[u1.id, u2.id])
    assert counts == {u1.id: 2, u2.id: 1}
    assert crud_user.order_counts_for(db_session, user_ids=[]) == {}


def test_order_metrics_excludes_cancelled_from_spend(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory(email="metrics@example.com")
    product = product_factory(stock=50, price_usd=10.0, price_pln=40.0)

    _place_order(
        db_session, user, product, qty=2, currency="USD", shipping_method_id=test_shipping_method_id
    )  # 20 USD
    pln = _place_order(
        db_session, user, product, qty=1, currency="PLN", shipping_method_id=test_shipping_method_id
    )  # 40 PLN
    cancelled = _place_order(
        db_session, user, product, qty=1, currency="USD", shipping_method_id=test_shipping_method_id
    )  # 10 USD
    crud_order.cancel(db_session, order=cancelled)
    db_session.flush()

    metrics = crud_user.order_metrics(db_session, user_id=user.id)
    assert metrics["order_count"] == 3  # all orders, incl. cancelled
    assert metrics["total_spent"]["USD"] == 20.0  # cancelled 10 excluded
    assert metrics["total_spent"]["PLN"] == 40.0
    assert metrics["last_order_at"] is not None
    assert pln.id is not None


def test_order_metrics_empty(db_session, user_factory):
    user = user_factory(email="noorders@example.com")
    metrics = crud_user.order_metrics(db_session, user_id=user.id)
    assert metrics["order_count"] == 0
    assert metrics["total_spent"] == {}
    assert metrics["last_order_at"] is None


def test_unlock_clears_lockout(db_session, user_factory):
    from datetime import UTC, datetime, timedelta

    user = user_factory(email="locked@example.com")
    user.failed_login_attempts = 10
    user.locked_until = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=30)
    db_session.add(user)
    db_session.flush()
    assert user.is_locked is True

    crud_user.unlock(db_session, user=user)
    db_session.flush()
    assert user.failed_login_attempts == 0
    assert user.locked_until is None
    assert user.is_locked is False
