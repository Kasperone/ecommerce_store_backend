"""Reverse charge on the admin conversion paths — Phase-0 tax U4-tail.

Draft-order conversion resolves RC the same way checkout does (owner + EU
destination + a fresh VIES confirmation). Quote conversion defers its address to
fulfilment, so it has no destination at conversion and is domestic by
construction — the model default already gives that, and this file documents it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models.company_user import CompanyRole
from app.models.order import Order, OrderItem
from app.models.user import UserRole
from app.services import order_email
from tests.factories.company import CompanyFactory
from tests.factories.company_user import CompanyUserFactory
from tests.integration.test_admin_draft_orders_convert import (
    _convert,
    _ready_draft,
    _seed_rated_method,
)

pytestmark = pytest.mark.integration


def _fresh() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)


@pytest.fixture
def company_factory(db_session: Session):
    CompanyFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]
    return lambda **kw: CompanyFactory(**kw)


@pytest.fixture
def company_user_factory(db_session: Session):
    CompanyUserFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CompanyUserFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]
    return lambda **kw: CompanyUserFactory(**kw)


@pytest.fixture
def scheduled_emails(monkeypatch) -> list:
    """Stub the route's background email (the real task can't see savepoint rows)."""
    calls: list = []
    monkeypatch.setattr(
        order_email,
        "send_order_event_in_background",
        lambda order_id, event: calls.append((order_id, event)),
    )
    return calls


def _eu_member(user_factory, company_factory, company_user_factory, email):
    owner = user_factory(email=email, role=UserRole.CUSTOMER)
    company = company_factory(
        name="EU Buyer Co", vat_id="DE811907980", vies_valid=True, vies_confirmed_at=_fresh()
    )
    company_user_factory(
        company_id=company.id, user_id=owner.id, role=CompanyRole.COMPANY_ADMIN.value
    )
    return owner


async def test_draft_convert_applies_reverse_charge(
    client,
    user_factory,
    product_factory,
    company_factory,
    company_user_factory,
    db_session,
    scheduled_emails,
):
    admin = user_factory(email="rct-admin@example.com", role=UserRole.ADMIN)
    owner = _eu_member(user_factory, company_factory, company_user_factory, "rct-owner@example.com")
    product = product_factory(stock=10, price_pln=149.0, vat_rate=23)
    method = _seed_rated_method(db_session, code="courier-pl", amount=0.0, currency="PLN")

    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=method.id,
        owner={"user_id": owner.id, "currency": "PLN"},
        items=[{"product_id": product.id, "quantity": 1}],
        header={"shipping_country": "DE"},  # EU, non-home → reverse charge
    )
    resp = await _convert(client, admin, draft["id"])
    assert resp.status_code == 201, resp.text

    order = db_session.query(Order).filter(Order.id == resp.json()["id"]).one()
    assert order.tax_treatment == "reverse_charge"
    assert order.buyer_vat_id == "DE811907980"
    item = db_session.query(OrderItem).filter(OrderItem.order_id == order.id).one()
    assert item.unit_price == Decimal("121.14")  # 149,00 gross → net
    assert item.vat_rate == 0


async def test_draft_convert_domestic_when_home_destination(
    client,
    user_factory,
    product_factory,
    company_factory,
    company_user_factory,
    db_session,
    scheduled_emails,
):
    # Same confirmed EU buyer, but shipping home (PL) → domestic: gross + 23%.
    admin = user_factory(email="rct-admin2@example.com", role=UserRole.ADMIN)
    owner = _eu_member(
        user_factory, company_factory, company_user_factory, "rct-owner2@example.com"
    )
    product = product_factory(stock=10, price_pln=149.0, vat_rate=23)
    method = _seed_rated_method(db_session, code="courier-pl2", amount=0.0, currency="PLN")

    draft = await _ready_draft(
        client,
        admin,
        shipping_method_id=method.id,
        owner={"user_id": owner.id, "currency": "PLN"},
        items=[{"product_id": product.id, "quantity": 1}],
        header={"shipping_country": "PL"},
    )
    resp = await _convert(client, admin, draft["id"])
    assert resp.status_code == 201, resp.text

    order = db_session.query(Order).filter(Order.id == resp.json()["id"]).one()
    assert order.tax_treatment == "domestic"
    item = db_session.query(OrderItem).filter(OrderItem.order_id == order.id).one()
    assert item.unit_price == Decimal("149.00")
    assert item.vat_rate == 23
