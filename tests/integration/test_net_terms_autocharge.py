"""Integration tests for B2B net-terms auto-charge (tail Program C).

The cron off-session-charges a vaulted card for due, unpaid net-terms orders of
companies that opted into auto-pay. Stripe is mocked via an injected fake service
(``charge_due_orders(db, service=fake)``) so the money-path logic — gating,
settlement, decline handling, idempotency-by-latch — is exercised without network.
The live off-session charge is verified separately against the Stripe sandbox.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.crud import order_event as crud_order_event
from app.models.order import Order, OrderStatus
from app.services.net_terms_autocharge import (
    CHARGED_EVENT_KIND,
    FAILED_EVENT_KIND,
    charge_due_orders,
)
from app.services.stripe import StripeError
from tests.factories.company import CompanyFactory

pytestmark = pytest.mark.integration


class _FakeStripe:
    """Stand-in StripeService: records calls, returns a status or raises."""

    def __init__(self, *, status: str = "succeeded", fail: bool = False) -> None:
        self._status = status
        self._fail = fail
        self.calls: list[dict] = []

    def charge_off_session(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self._fail:
            raise StripeError("card declined")
        return {"status": self._status, "id": "pi_test_123"}


@pytest.fixture
def company_factory(db_session: Session):
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs):
        return CompanyFactory(**kwargs)

    return _make


def _autopay_company(company_factory, *, enabled=True, card=True):
    return company_factory(
        name="AutoPayCo",
        net_terms_enabled=True,
        default_currency="USD",
        auto_charge_enabled=enabled,
        stripe_customer_id="cus_test" if card else None,
        auto_charge_payment_method_id="pm_test" if card else None,
    )


def _make_order(
    db,
    company,
    *,
    order_number,
    due_at,
    paid_at=None,
    status=OrderStatus.PENDING,
    gift_card_amount=Decimal("0"),
    store_credit_amount=Decimal("0"),
):
    order = Order(
        order_number=order_number,
        status=status,
        subtotal=100.0,
        total=100.0,
        gift_card_amount=gift_card_amount,
        store_credit_amount=store_credit_amount,
        currency="USD",
        shipping_address="1 Main St",
        shipping_city="Warsaw",
        shipping_postal_code="00-001",
        shipping_country="PL",
        guest_email="ac@example.com",
        payment_method="net_terms",
        company_id=company.id,
        payment_due_at=due_at,
        net_terms_paid_at=paid_at,
    )
    db.add(order)
    db.commit()
    return order


def _past() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)


def _future() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) + timedelta(days=5)


def test_due_order_is_charged_and_settled(db_session, company_factory):
    company = _autopay_company(company_factory)
    order = _make_order(db_session, company, order_number="AC-1", due_at=_past())
    fake = _FakeStripe()
    assert charge_due_orders(db_session, service=fake) == 1
    assert len(fake.calls) == 1
    assert fake.calls[0]["amount_minor"] == 10000  # 100.00 USD
    db_session.refresh(order)
    assert order.net_terms_paid_at is not None
    assert order.status == OrderStatus.PENDING  # payment tracked orthogonally
    events = crud_order_event.list_for_order(db_session, order_id=order.id)
    assert any(e.kind == CHARGED_EVENT_KIND for e in events)


def test_partial_stored_value_charges_amount_due_not_total(db_session, company_factory):
    """SEC-018: gift-card/store-credit tender is already collected — bill the remainder.

    Charging ``total`` here would bill the buyer a second time for the covered part.
    """
    company = _autopay_company(company_factory)
    order = _make_order(
        db_session,
        company,
        order_number="AC-GC",
        due_at=_past(),
        gift_card_amount=Decimal("30.00"),
        store_credit_amount=Decimal("5.50"),
    )
    fake = _FakeStripe()
    assert charge_due_orders(db_session, service=fake) == 1
    assert len(fake.calls) == 1
    # 100.00 total - 30.00 gift card - 5.50 store credit = 64.50 USD
    assert fake.calls[0]["amount_minor"] == 6450
    db_session.refresh(order)
    assert order.net_terms_paid_at is not None


def test_fully_covered_by_stored_value_skips_stripe(db_session, company_factory):
    """SEC-018: nothing owed → settle inline, never hand Stripe a zero-amount charge."""
    company = _autopay_company(company_factory)
    order = _make_order(
        db_session,
        company,
        order_number="AC-COVERED",
        due_at=_past(),
        gift_card_amount=Decimal("60.00"),
        store_credit_amount=Decimal("40.00"),
    )
    fake = _FakeStripe()
    assert charge_due_orders(db_session, service=fake) == 1
    assert fake.calls == []  # no Stripe call at all
    db_session.refresh(order)
    assert order.net_terms_paid_at is not None
    assert order.status == OrderStatus.PENDING  # payment tracked orthogonally
    events = crud_order_event.list_for_order(db_session, order_id=order.id)
    charged_events = [e for e in events if e.kind == CHARGED_EVENT_KIND]
    assert len(charged_events) == 1
    assert charged_events[0].payload == {"reason": "covered_by_stored_value"}


def test_not_yet_due_not_charged(db_session, company_factory):
    company = _autopay_company(company_factory)
    _make_order(db_session, company, order_number="AC-FUT", due_at=_future())
    fake = _FakeStripe()
    assert charge_due_orders(db_session, service=fake) == 0
    assert fake.calls == []


def test_already_paid_not_charged(db_session, company_factory):
    company = _autopay_company(company_factory)
    _make_order(
        db_session,
        company,
        order_number="AC-PAID",
        due_at=_past(),
        paid_at=datetime.now(UTC).replace(tzinfo=None),
    )
    fake = _FakeStripe()
    assert charge_due_orders(db_session, service=fake) == 0
    assert fake.calls == []


def test_no_vaulted_card_not_charged(db_session, company_factory):
    company = _autopay_company(company_factory, card=False)
    _make_order(db_session, company, order_number="AC-NOCARD", due_at=_past())
    fake = _FakeStripe()
    assert charge_due_orders(db_session, service=fake) == 0
    assert fake.calls == []


def test_auto_charge_disabled_not_charged(db_session, company_factory):
    company = _autopay_company(company_factory, enabled=False)
    _make_order(db_session, company, order_number="AC-OFF", due_at=_past())
    fake = _FakeStripe()
    assert charge_due_orders(db_session, service=fake) == 0
    assert fake.calls == []


def test_declined_card_leaves_unpaid_and_records_failure(db_session, company_factory):
    company = _autopay_company(company_factory)
    order = _make_order(db_session, company, order_number="AC-DECLINE", due_at=_past())
    fake = _FakeStripe(fail=True)
    assert charge_due_orders(db_session, service=fake) == 0
    db_session.refresh(order)
    assert order.net_terms_paid_at is None
    events = crud_order_event.list_for_order(db_session, order_id=order.id)
    assert any(e.kind == FAILED_EVENT_KIND for e in events)


def test_requires_action_not_settled(db_session, company_factory):
    company = _autopay_company(company_factory)
    order = _make_order(db_session, company, order_number="AC-RA", due_at=_past())
    fake = _FakeStripe(status="requires_action")
    assert charge_due_orders(db_session, service=fake) == 0
    db_session.refresh(order)
    assert order.net_terms_paid_at is None
    events = crud_order_event.list_for_order(db_session, order_id=order.id)
    assert any(e.kind == FAILED_EVENT_KIND for e in events)


def test_stripe_unconfigured_is_noop(db_session, company_factory):
    company = _autopay_company(company_factory)
    _make_order(db_session, company, order_number="AC-NOSTRIPE", due_at=_past())
    # No injected service + no Stripe keys in the test env → healthy no-op.
    assert charge_due_orders(db_session) == 0


async def test_credit_summary_exposes_auto_charge_flags(client, user_factory, company_factory):
    """The buyer's credit summary carries auto_charge_enabled + card-set status so
    the portal can render the 'set up auto-pay' card without the Stripe handles."""
    from app.core.security import create_access_token

    buyer = user_factory(email="ac-portal@example.com")
    company_factory(
        name="AcPortalCo",
        primary_user_id=buyer.id,
        net_terms_enabled=True,
        default_currency="USD",
        auto_charge_enabled=True,
        stripe_customer_id="cus_x",
        auto_charge_payment_method_id="pm_x",
    )
    token, _, _ = create_access_token(subject=str(buyer.id))
    resp = await client.get(
        "/api/v1/companies/me/credit", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["auto_charge_enabled"] is True
    assert body["auto_charge_card_set"] is True
