"""Reverse charge — Phase-0 tax U4.

The money-path behaviours whose failure would silently mis-tax a B2B EU sale:
the RC gate (`resolve_tax_context`), net pricing through the real checkout path
(`create_with_items`), and the faktura's 0%-group + annotation.
"""

from __future__ import annotations

import datetime as dt
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.money import net_of_gross
from app.crud import order as crud_order
from app.models.company_user import CompanyRole
from app.schemas.order import OrderCreate, OrderItemCreate
from app.services import tax as tax_service
from app.services.invoice import StoreIdentity, build_invoice_pdf, compute_vat_breakdown
from tests.factories.company import CompanyFactory
from tests.factories.company_user import CompanyUserFactory

pytestmark = pytest.mark.integration


def _utcnow() -> datetime:
    return datetime.now(dt.UTC).replace(tzinfo=None)


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


def _confirmed_member(user_factory, company_factory, company_user_factory, email, **company_kw):
    """A user who is an active COMPANY_ADMIN of a VIES-confirmed company."""
    from app.models.user import UserRole

    user = user_factory(email=email, role=UserRole.CUSTOMER)
    defaults = dict(
        vat_id="DE811907980",
        vies_valid=True,
        vies_confirmed_at=_utcnow() - timedelta(days=1),
    )
    defaults.update(company_kw)
    company = company_factory(name="EU Buyer Co", **defaults)
    company_user_factory(
        company_id=company.id, user_id=user.id, role=CompanyRole.COMPANY_ADMIN.value
    )
    return user, company


def _order_in(product_id: int, shipping_method_id: int, *, country: str) -> OrderCreate:
    return OrderCreate(
        items=[OrderItemCreate(product_id=product_id, quantity=1)],
        currency="PLN",
        shipping_address="1 Test St",
        shipping_city="Testville",
        shipping_postal_code="00-001",
        shipping_country=country,
        shipping_method_id=shipping_method_id,
    )


# --- net_of_gross -----------------------------------------------------------


def test_net_of_gross_strips_vat():
    # 149,00 zł gross @23% → 121,14 zł net (decision 4).
    assert net_of_gross(Decimal("149.00"), 23) == Decimal("121.14")


def test_net_of_gross_zero_rate_is_identity():
    assert net_of_gross(Decimal("100.00"), 0) == Decimal("100.00")


# --- resolve_tax_context gate ----------------------------------------------


class TestResolveTaxContext:
    def test_guest_is_domestic(self, db_session):
        ctx = tax_service.resolve_tax_context(db_session, user_id=None, shipping_country="DE")
        assert ctx.treatment == tax_service.TREATMENT_DOMESTIC

    def test_home_country_is_domestic(
        self, db_session, user_factory, company_factory, company_user_factory
    ):
        user, _ = _confirmed_member(
            user_factory, company_factory, company_user_factory, "rc-home@example.com"
        )
        ctx = tax_service.resolve_tax_context(db_session, user_id=user.id, shipping_country="PL")
        assert ctx.treatment == tax_service.TREATMENT_DOMESTIC

    def test_non_eu_is_domestic(
        self, db_session, user_factory, company_factory, company_user_factory
    ):
        user, _ = _confirmed_member(
            user_factory, company_factory, company_user_factory, "rc-us@example.com"
        )
        ctx = tax_service.resolve_tax_context(db_session, user_id=user.id, shipping_country="US")
        assert ctx.treatment == tax_service.TREATMENT_DOMESTIC

    def test_non_member_is_domestic(self, db_session, user_factory):
        from app.models.user import UserRole

        user = user_factory(email="rc-nomember@example.com", role=UserRole.CUSTOMER)
        ctx = tax_service.resolve_tax_context(db_session, user_id=user.id, shipping_country="DE")
        assert ctx.treatment == tax_service.TREATMENT_DOMESTIC

    def test_unconfirmed_company_is_domestic(
        self, db_session, user_factory, company_factory, company_user_factory
    ):
        user, _ = _confirmed_member(
            user_factory,
            company_factory,
            company_user_factory,
            "rc-unconf@example.com",
            vies_valid=False,
            vies_confirmed_at=None,
        )
        ctx = tax_service.resolve_tax_context(db_session, user_id=user.id, shipping_country="DE")
        assert ctx.treatment == tax_service.TREATMENT_DOMESTIC

    def test_stale_confirmation_is_domestic(
        self, db_session, user_factory, company_factory, company_user_factory
    ):
        user, _ = _confirmed_member(
            user_factory,
            company_factory,
            company_user_factory,
            "rc-stale@example.com",
            vies_confirmed_at=_utcnow() - timedelta(days=200),
        )
        ctx = tax_service.resolve_tax_context(db_session, user_id=user.id, shipping_country="DE")
        assert ctx.treatment == tax_service.TREATMENT_DOMESTIC

    def test_confirmed_eu_member_is_reverse_charge(
        self, db_session, user_factory, company_factory, company_user_factory
    ):
        user, company = _confirmed_member(
            user_factory, company_factory, company_user_factory, "rc-yes@example.com"
        )
        ctx = tax_service.resolve_tax_context(db_session, user_id=user.id, shipping_country="DE")
        assert ctx.treatment == tax_service.TREATMENT_REVERSE_CHARGE
        assert ctx.buyer_vat_id == company.vat_id
        assert ctx.buyer_vies_confirmed_at == company.vies_confirmed_at


# --- full checkout money path ----------------------------------------------


def test_checkout_applies_reverse_charge(
    db_session,
    user_factory,
    company_factory,
    company_user_factory,
    product_factory,
    test_shipping_method_id,
):
    user, company = _confirmed_member(
        user_factory, company_factory, company_user_factory, "rc-buy@example.com"
    )
    product = product_factory(stock=10, price_pln=149.0, vat_rate=23)
    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_order_in(product.id, test_shipping_method_id, country="DE"),
    )
    assert order.tax_treatment == tax_service.TREATMENT_REVERSE_CHARGE
    assert order.buyer_vat_id == company.vat_id
    assert order.buyer_vies_confirmed_at is not None
    line = order.items[0]
    # VAT stripped: gross 149,00 → net 121,14; line snapshots 0%.
    assert line.unit_price == Decimal("121.14")
    assert line.vat_rate == 0
    assert line.total_price == Decimal("121.14")  # unit x qty holds exactly
    assert order.subtotal == Decimal("121.14")


def test_checkout_domestic_keeps_gross(
    db_session,
    user_factory,
    company_factory,
    company_user_factory,
    product_factory,
    test_shipping_method_id,
):
    # Same B2B buyer, but shipping home (PL) → domestic: gross price, 23% snapshot.
    user, _ = _confirmed_member(
        user_factory, company_factory, company_user_factory, "rc-dom@example.com"
    )
    product = product_factory(stock=10, price_pln=149.0, vat_rate=23)
    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_order_in(product.id, test_shipping_method_id, country="PL"),
    )
    assert order.tax_treatment == tax_service.TREATMENT_DOMESTIC
    assert order.buyer_vat_id is None
    line = order.items[0]
    assert line.unit_price == Decimal("149.00")
    assert line.vat_rate == 23


# --- faktura ----------------------------------------------------------------


def test_reverse_charge_faktura_breakdown_and_render(
    db_session,
    user_factory,
    company_factory,
    company_user_factory,
    product_factory,
    test_shipping_method_id,
):
    user, _ = _confirmed_member(
        user_factory, company_factory, company_user_factory, "rc-fak@example.com"
    )
    product = product_factory(stock=10, price_pln=149.0, vat_rate=23)
    order = crud_order.create_with_items(
        db_session,
        user_id=user.id,
        obj_in=_order_in(product.id, test_shipping_method_id, country="DE"),
    )
    # Single 0% VAT group — compute_vat_breakdown is unchanged; the 0% lines just
    # fall through it (shipping is free here, so no separate shipping group).
    groups = compute_vat_breakdown(order, shipping_vat_rate=0)
    assert [g.rate for g in groups] == [0]
    assert groups[0].vat == Decimal("0.00")

    store = StoreIdentity(name="Shop", tax_id="1234567890", vat_id="PL1234567890")
    pdf = build_invoice_pdf(
        order, store=store, invoice_number="FV/2026/1", issued_at=dt.date(2026, 7, 22), lang="pl"
    )
    assert isinstance(pdf, bytes) and len(pdf) > 800
