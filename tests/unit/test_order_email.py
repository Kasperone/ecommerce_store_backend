"""Unit tests for the order-email service (composition + background entry).

No HTTP, no real DB — orders are transient ORM instances, the send is captured
by the autouse ``captured_emails`` fixture, and the background entry's session +
lookup are monkeypatched.
"""

from __future__ import annotations

import datetime as dt
from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.core import email
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


class _StubKor:
    """KOR-shaped placeholder used by the refunded-event composition tests."""

    kor_number = "KOR/2026/00007"


# The composition tests stub the renderer, so the session is never touched —
# cast a dummy to satisfy the typed ``send_order_event(db: Session, ...)`` param.
_DB = cast(Session, _DummySession())


@pytest.fixture(autouse=True)
def _stub_render(monkeypatch):
    """Stub the DB-backed invoice + KOR renderers for these composition tests.

    ``send_order_event`` resolves faktura-vs-proforma via the DB and (for the
    refunded event) the KOR registry; the renderers themselves are covered by
    the invoice + integration tests, so here we only assert that *a* PDF is
    attached when the spec says so.
    """
    monkeypatch.setattr(order_email, "render_invoice_pdf", lambda *a, **k: b"%PDF-stub")
    monkeypatch.setattr(
        order_email, "render_corrective_invoice_pdf", lambda *a, **k: b"%PDF-kor-stub"
    )

    # The store identity is now resolved from the DB-backed ``store_config``
    # singleton (``StoreIdentity.from_store_config``); these composition tests
    # use a dummy session, so stub the classmethod to a fixed identity (its own
    # config-vs-env fallback is covered in ``test_store_identity.py``).
    def _stub_identity(db, settings) -> order_email.StoreIdentity:
        return order_email.StoreIdentity(name="Test Store")

    monkeypatch.setattr(order_email.StoreIdentity, "from_store_config", _stub_identity)
    # The refunded event composition reads the KOR registry via
    # ``crud_corrective.get_by_refund_id``; stub it out to a transient KOR-shaped
    # object so the test doesn't need a DB row.
    monkeypatch.setattr(
        order_email.crud_corrective,
        "get_by_refund_id",
        lambda db, refund_id: _StubKor(),
    )


# --- event_for_status -------------------------------------------------------


@pytest.mark.parametrize(
    "order_status,event",
    [("paid", "paid"), ("shipped", "shipped"), ("delivered", "delivered")],
)
def test_event_for_status_maps_notifiable(order_status, event):
    assert order_email.event_for_status(order_status) == event


@pytest.mark.parametrize("order_status", ["pending", "processing", "cancelled", "refunded", "x"])
def test_event_for_status_none_for_others(order_status):
    # ``refunded`` is intentionally NOT routed via the admin status PATCH — it
    # fires via the dedicated refund endpoint instead (mirrors how cancel is
    # excluded from the PATCH-driven event map).
    assert order_email.event_for_status(order_status) is None


# --- send_order_event composition -------------------------------------------


def test_confirmation_attaches_invoice(captured_emails):
    order_email.send_order_event(_DB, _make_order(), "confirmation")
    assert len(captured_emails) == 1
    entry = captured_emails[0]
    assert entry["kind"] == "order_email"
    assert entry["email_to"] == "buyer@example.com"
    assert "confirmed" in entry["subject"]
    assert entry["invoice_pdf"][:4] == b"%PDF"


def test_paid_attaches_invoice(captured_emails):
    order_email.send_order_event(_DB, _make_order(), "paid")
    assert captured_emails[0]["invoice_pdf"][:4] == b"%PDF"


@pytest.mark.parametrize("event", ["shipped", "delivered", "cancelled"])
def test_status_events_have_no_attachment(captured_emails, event):
    order_email.send_order_event(_DB, _make_order(), event)
    assert len(captured_emails) == 1
    assert captured_emails[0]["invoice_pdf"] is None
    # Non-refund events don't carry a KOR.
    assert captured_emails[0].get("corrective_invoice_pdf") is None


def test_refunded_attaches_kor_and_no_invoice(captured_emails):
    """The refunded event attaches the KOR PDF (not the faktura) + its number."""
    order = _make_order()
    # Provide a refund row on the order so the spec can pick "latest" by index.
    from app.models.refund import Refund

    order.refunds = [
        Refund(order_id=order.id, amount=order.total, currency=order.currency, reason="other")
    ]

    order_email.send_order_event(_DB, order, "refunded")
    assert len(captured_emails) == 1
    entry = captured_emails[0]
    assert entry["kind"] == "order_email"
    assert "Refund" in entry["subject"]
    # KOR is attached; the invoice is NOT (refunded skips _ATTACH_EVENTS).
    assert entry["invoice_pdf"] is None
    assert entry["corrective_invoice_pdf"][:4] == b"%PDF"
    assert entry["corrective_invoice_number"] == "KOR/2026/00007"


def test_refunded_with_no_refunds_attaches_no_kor(captured_emails):
    """Defensive: a refunded event for an order with no refunds (shouldn't happen
    in practice — the route writes the refund before firing the email — but the
    composition path must not crash)."""
    order = _make_order()
    order.refunds = []  # explicitly empty

    order_email.send_order_event(_DB, order, "refunded")
    entry = captured_emails[0]
    assert entry["corrective_invoice_pdf"] is None
    assert entry["corrective_invoice_number"] is None


def test_unknown_event_sends_nothing(captured_emails):
    order_email.send_order_event(_DB, _make_order(), "bogus")
    assert captured_emails == []


def test_send_failure_is_swallowed(captured_emails, monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("resend down")

    monkeypatch.setattr(order_email.email, "send_order_email", _boom)
    # Best-effort: a failed send must not bubble out of send_order_event.
    order_email.send_order_event(_DB, _make_order(), "shipped")


# --- localization -----------------------------------------------------------


def test_polish_user_gets_polish_subject_and_lang(captured_emails):
    order = _make_order()
    order.user.locale = "pl"
    order_email.send_order_event(_DB, order, "confirmation")
    entry = captured_emails[0]
    assert "potwierdzone" in entry["subject"]  # PL "confirmed"
    assert entry["lang"] == "pl"


def test_unknown_locale_falls_back_to_english(captured_emails):
    order = _make_order()
    order.user.locale = "de"  # unsupported → EN copy
    order_email.send_order_event(_DB, order, "shipped")
    assert "shipped" in captured_emails[0]["subject"]


def test_render_order_email_body_english_labels():
    html, text = email.render_order_email_body(
        order=_make_order(), heading="Hi", intro_html="<p>x</p>", lang="en"
    )
    for label in ("Order", "Subtotal", "Shipping", "Total", "Shipping address"):
        assert label in html
    assert "Total:" in text


def test_render_order_email_body_polish_labels():
    html, text = email.render_order_email_body(
        order=_make_order(), heading="Cześć", intro_html="<p>x</p>", lang="pl"
    )
    for label in ("Zamówienie", "Suma częściowa", "Dostawa", "Razem", "Adres dostawy"):
        assert label in html
    assert "Razem:" in text


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
