"""Unit tests for the order-email service (composition + background entry).

No HTTP, no real DB — orders are transient ORM instances, the send is captured
by the autouse ``captured_emails`` fixture, and the background entry's session +
lookup are monkeypatched.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.models.order import Order, OrderItem, OrderStatus
from app.models.user import User
from app.services import order_email


def _make_order() -> Order:
    user = User(email="buyer@example.com", first_name="Buyer", last_name="One")
    order = Order(
        order_number="ORD-20260527-EMAIL1",
        status=OrderStatus.PENDING,
        subtotal=100.0,
        tax=0.0,
        shipping=0.0,
        total=100.0,
        currency="USD",
        shipping_address="1 Test St",
        shipping_city="Town",
        shipping_postal_code="00-001",
        shipping_country="USA",
        created_at=dt.datetime(2026, 5, 27, 12, 0),
    )
    order.id = 42
    order.user = user
    order.items = [
        OrderItem(
            product_name="Widget",
            product_sku="SKU-1",
            quantity=1,
            unit_price=100.0,
            total_price=100.0,
        ),
    ]
    return order


class _DummySession:
    def close(self) -> None:
        pass


# --- event_for_status -------------------------------------------------------


@pytest.mark.parametrize(
    "order_status,event",
    [("paid", "paid"), ("shipped", "shipped"), ("delivered", "delivered")],
)
def test_event_for_status_maps_notifiable(order_status, event):
    assert order_email.event_for_status(order_status) == event


@pytest.mark.parametrize("order_status", ["pending", "processing", "cancelled", "refunded", "x"])
def test_event_for_status_none_for_others(order_status):
    assert order_email.event_for_status(order_status) is None


# --- send_order_event composition -------------------------------------------


def test_confirmation_attaches_invoice(captured_emails):
    order_email.send_order_event(_make_order(), "confirmation")
    assert len(captured_emails) == 1
    entry = captured_emails[0]
    assert entry["kind"] == "order_email"
    assert entry["email_to"] == "buyer@example.com"
    assert "confirmed" in entry["subject"]
    assert entry["invoice_pdf"][:4] == b"%PDF"


def test_paid_attaches_invoice(captured_emails):
    order_email.send_order_event(_make_order(), "paid")
    assert captured_emails[0]["invoice_pdf"][:4] == b"%PDF"


@pytest.mark.parametrize("event", ["shipped", "delivered", "cancelled"])
def test_status_events_have_no_attachment(captured_emails, event):
    order_email.send_order_event(_make_order(), event)
    assert len(captured_emails) == 1
    assert captured_emails[0]["invoice_pdf"] is None


def test_unknown_event_sends_nothing(captured_emails):
    order_email.send_order_event(_make_order(), "bogus")
    assert captured_emails == []


def test_send_failure_is_swallowed(captured_emails, monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("resend down")

    monkeypatch.setattr(order_email.email, "send_order_email", _boom)
    # Best-effort: a failed send must not bubble out of send_order_event.
    order_email.send_order_event(_make_order(), "shipped")


# --- background entry -------------------------------------------------------


def test_background_entry_sends_when_found(captured_emails, monkeypatch):
    order = _make_order()
    monkeypatch.setattr(order_email, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(order_email.crud_order, "get", lambda db, id: order)
    order_email.send_order_event_in_background(order.id, "shipped")
    assert len(captured_emails) == 1
    assert captured_emails[0]["kind"] == "order_email"


def test_background_entry_noop_when_missing(captured_emails, monkeypatch):
    monkeypatch.setattr(order_email, "SessionLocal", lambda: _DummySession())
    monkeypatch.setattr(order_email.crud_order, "get", lambda db, id: None)
    order_email.send_order_event_in_background(999, "shipped")
    assert captured_emails == []
