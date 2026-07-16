"""Integration tests for B2B net-terms dunning (overdue reminders, tail Program B).

The detection pass emails the buyer of each overdue, unpaid, non-cancelled
net-terms order, deduped per order via a ``net_terms_reminder`` order_event and
throttled to once per ``NET_TERMS_DUNNING_INTERVAL_DAYS``. Dark by default behind
``settings.NET_TERMS_DUNNING_ENABLED``. Emails are captured by the autouse
``captured_emails`` fixture (patches ``email.send_email``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.models.order import Order, OrderStatus
from app.services.net_terms_dunning import REMINDER_EVENT_KIND, send_net_terms_reminders

pytestmark = pytest.mark.integration


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _make_net_terms_order(
    db,
    user,
    *,
    order_number: str,
    due_at: datetime,
    paid_at: datetime | None = None,
    status: OrderStatus = OrderStatus.PENDING,
    currency: str = "USD",
) -> Order:
    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=status,
        subtotal=100.0,
        total=100.0,
        currency=currency,
        shipping_address="1 Main St",
        shipping_city="Warsaw",
        shipping_postal_code="00-001",
        shipping_country="PL",
        payment_method="net_terms",
        payment_due_at=due_at,
        net_terms_paid_at=paid_at,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


@pytest.fixture
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "NET_TERMS_DUNNING_ENABLED", True)


def test_disabled_flag_sends_nothing(db_session, user_factory, captured_emails):
    user = user_factory(email="dun-off@example.com")
    _make_net_terms_order(
        db_session, user, order_number="NT-OFF-1", due_at=_naive_now() - timedelta(days=3)
    )
    # settings.NET_TERMS_DUNNING_ENABLED defaults False — leave it.
    assert send_net_terms_reminders(db_session) == 0
    assert captured_emails == []


def test_not_overdue_not_sent(db_session, user_factory, captured_emails, _enabled):
    user = user_factory(email="dun-future@example.com")
    _make_net_terms_order(
        db_session, user, order_number="NT-FUT-1", due_at=_naive_now() + timedelta(days=5)
    )
    assert send_net_terms_reminders(db_session) == 0
    assert captured_emails == []


def test_paid_not_sent(db_session, user_factory, captured_emails, _enabled):
    user = user_factory(email="dun-paid@example.com")
    _make_net_terms_order(
        db_session,
        user,
        order_number="NT-PAID-1",
        due_at=_naive_now() - timedelta(days=3),
        paid_at=_naive_now(),
    )
    assert send_net_terms_reminders(db_session) == 0
    assert captured_emails == []


def test_cancelled_not_sent(db_session, user_factory, captured_emails, _enabled):
    user = user_factory(email="dun-cancelled@example.com")
    _make_net_terms_order(
        db_session,
        user,
        order_number="NT-CAN-1",
        due_at=_naive_now() - timedelta(days=3),
        status=OrderStatus.CANCELLED,
    )
    assert send_net_terms_reminders(db_session) == 0
    assert captured_emails == []


def test_overdue_sends_once_and_dedups(db_session, user_factory, captured_emails, _enabled):
    user = user_factory(email="dun-overdue@example.com")
    order = _make_net_terms_order(
        db_session, user, order_number="NT-OD-1", due_at=_naive_now() - timedelta(days=4)
    )
    assert send_net_terms_reminders(db_session) == 1
    assert len(captured_emails) == 1
    assert captured_emails[0]["email_to"] == user.email
    assert "NT-OD-1" in captured_emails[0]["html_content"]

    # A reminder order_event was written (the dedup latch).
    from app.crud import order_event as crud_order_event

    events = crud_order_event.list_for_order(db_session, order_id=order.id)
    assert any(e.kind == REMINDER_EVENT_KIND for e in events)

    # Second run within the throttle window → no second email.
    assert send_net_terms_reminders(db_session) == 0
    assert len(captured_emails) == 1


def test_pl_locale_copy(db_session, user_factory, captured_emails, _enabled):
    user = user_factory(email="dun-pl@example.com", locale="pl")
    _make_net_terms_order(
        db_session, user, order_number="NT-PL-1", due_at=_naive_now() - timedelta(days=2)
    )
    assert send_net_terms_reminders(db_session) == 1
    sent = captured_emails[0]
    assert "przeterminowana" in sent["subject"]
    assert "Zaległa płatność" in sent["html_content"]
