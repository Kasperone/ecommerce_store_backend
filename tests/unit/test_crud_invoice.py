"""Unit tests for the invoice-numbering CRUD (DB-backed, no HTTP).

Covers ``issue_for_order`` idempotency, gap-free sequential allocation across
orders, the per-year counter, and issuance on the ``mark_paid`` transition.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.crud import invoice as crud_invoice
from app.crud import order as crud_order
from app.models.order import OrderStatus
from app.schemas.order import OrderCreate, OrderItemCreate

pytestmark = pytest.mark.integration


def _order_in(product_id: int, quantity: int = 1, currency: str = "USD") -> OrderCreate:
    return OrderCreate(
        items=[OrderItemCreate(product_id=product_id, quantity=quantity)],
        currency=currency,
        shipping_address="1 Test St",
        shipping_city="Testville",
        shipping_postal_code="00-001",
        shipping_country="Poland",
    )


def _make_order(db, user_factory, product_factory):
    user = user_factory()
    product = product_factory(stock=10, price_usd=20.0)
    return crud_order.create_with_items(db, user_id=user.id, obj_in=_order_in(product.id))


def test_issue_for_order_allocates_first_number(db_session, user_factory, product_factory):
    order = _make_order(db_session, user_factory, product_factory)

    inv = crud_invoice.issue_for_order(db_session, order=order)

    year = dt.datetime.now(dt.UTC).year
    assert inv.seq == 1
    assert inv.year == year
    assert inv.invoice_number == f"FV/{year}/00001"
    assert inv.order_id == order.id
    assert inv.issued_at is not None


def test_issue_for_order_is_idempotent(db_session, user_factory, product_factory):
    order = _make_order(db_session, user_factory, product_factory)

    first = crud_invoice.issue_for_order(db_session, order=order)
    second = crud_invoice.issue_for_order(db_session, order=order)

    # Same row reused — re-issuing never burns a number or changes the date.
    assert second.id == first.id
    assert second.invoice_number == first.invoice_number
    assert second.issued_at == first.issued_at
    assert second.seq == 1


def test_sequential_gap_free_across_orders(db_session, user_factory, product_factory):
    order_a = _make_order(db_session, user_factory, product_factory)
    order_b = _make_order(db_session, user_factory, product_factory)

    inv_a = crud_invoice.issue_for_order(db_session, order=order_a)
    inv_b = crud_invoice.issue_for_order(db_session, order=order_b)

    year = dt.datetime.now(dt.UTC).year
    assert inv_a.seq == 1
    assert inv_b.seq == 2
    assert inv_a.invoice_number == f"FV/{year}/00001"
    assert inv_b.invoice_number == f"FV/{year}/00002"


def test_get_by_order_id_none_before_issue(db_session, user_factory, product_factory):
    order = _make_order(db_session, user_factory, product_factory)
    assert crud_invoice.get_by_order_id(db_session, order.id) is None


def test_mark_paid_issues_invoice(db_session, user_factory, product_factory):
    order = _make_order(db_session, user_factory, product_factory)

    crud_order.mark_paid(
        db_session, order=order, payment_intent_id="pi_test", payment_method="stripe"
    )

    assert OrderStatus(order.status) == OrderStatus.PAID
    inv = crud_invoice.get_by_order_id(db_session, order.id)
    assert inv is not None
    assert inv.seq == 1


def test_mark_paid_twice_issues_one_invoice(db_session, user_factory, product_factory):
    order = _make_order(db_session, user_factory, product_factory)

    crud_order.mark_paid(db_session, order=order, payment_intent_id="pi_1", payment_method="payu")
    # Second call no-ops (already paid) → must not allocate a second number.
    crud_order.mark_paid(db_session, order=order, payment_intent_id="pi_2", payment_method="payu")

    inv = crud_invoice.get_by_order_id(db_session, order.id)
    assert inv is not None
    assert inv.seq == 1


def test_render_number_uses_custom_format(db_session, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "INVOICE_NUMBER_FORMAT", "{year}/{seq:04d}")
    monkeypatch.setattr(settings, "INVOICE_NUMBER_PREFIX", "X")

    assert crud_invoice._render_number(year=2026, month=5, seq=7) == "2026/0007"
