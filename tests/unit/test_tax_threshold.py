"""Unit tests for the EU distance-selling €10 000 threshold monitor (DB-backed).

Covers the qualifying-set filter (domestic + EU-non-home only; reverse-charge,
domestic-PL, non-EU, cancelled and pending all excluded), the net-of-VAT basis
(not gross), EUR conversion incl. the unconvertible-order surfacing, the
Shopify-style status bands, and the current-or-prior-year liability rule.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.crud import store_config as crud_store_config
from app.models.order import Order, OrderItem, OrderStatus
from app.services import tax_threshold
from app.services.tax import TREATMENT_DOMESTIC, TREATMENT_REVERSE_CHARGE

pytestmark = pytest.mark.integration

THIS_YEAR = datetime.now().year


def _set_fx(db, rates: dict) -> None:
    config = crud_store_config.get_singleton(db)
    config.fx_rates = rates
    db.add(config)
    db.commit()


def _order(
    db,
    *,
    product,
    gross,
    country: str,
    currency: str = "EUR",
    treatment: str = TREATMENT_DOMESTIC,
    status: OrderStatus = OrderStatus.PAID,
    vat_rate: int = 0,
    year: int = THIS_YEAR,
) -> Order:
    """Build + persist a minimal order with one line (gross = line total).

    ``vat_rate=0`` by default so ``net == gross`` and totals read cleanly; a
    non-zero rate exercises the net-of-VAT decomposition.
    """
    gross = Decimal(str(gross))
    order = Order(
        order_number=f"ORD-{uuid4().hex[:12]}",
        guest_email="g@example.com",
        status=status,
        subtotal=gross,
        total=gross,
        currency=currency,
        shipping_address="1 Test St",
        shipping_city="Testville",
        shipping_postal_code="00-001",
        shipping_country=country,
        tax_treatment=treatment,
    )
    order.created_at = datetime(year, 6, 1)
    order.items = [
        OrderItem(
            product_id=product.id,
            product_name=product.name,
            quantity=1,
            unit_price=gross,
            total_price=gross,
            vat_rate=vat_rate,
        )
    ]
    db.add(order)
    db.commit()
    return order


# ---------------------------------------------------------------------------
# Qualifying set
# ---------------------------------------------------------------------------


def test_only_domestic_eu_non_home_counts(db_session, product_factory):
    product = product_factory(stock=1000)
    # Counts: domestic sales to two EU states other than PL.
    _order(db_session, product=product, gross=1000, country="DE")
    _order(db_session, product=product, gross=500, country="FR")
    # Excluded, each for a different reason:
    _order(db_session, product=product, gross=9999, country="PL")  # home country
    _order(db_session, product=product, gross=9999, country="US")  # non-EU
    _order(
        db_session,
        product=product,
        gross=9999,
        country="DE",
        treatment=TREATMENT_REVERSE_CHARGE,
    )  # B2B reverse charge
    _order(db_session, product=product, gross=9999, country="DE", status=OrderStatus.CANCELLED)
    _order(db_session, product=product, gross=9999, country="DE", status=OrderStatus.PENDING)

    status = tax_threshold.eu_distance_selling_status(db_session)
    assert status.current.total_eur == Decimal("1500.00")
    assert status.current.unconvertible_count == 0
    assert status.current.status == tax_threshold.STATUS_NOT_APPROACHING


def test_net_of_vat_not_gross(db_session, product_factory):
    """A 123,00 gross line at 23% counts as its 100,00 net, not the gross."""
    product = product_factory(stock=10)
    _order(db_session, product=product, gross="123.00", country="DE", vat_rate=23)
    status = tax_threshold.eu_distance_selling_status(db_session)
    assert status.current.total_eur == Decimal("100.00")


# ---------------------------------------------------------------------------
# Currency conversion
# ---------------------------------------------------------------------------


def test_multi_currency_converted_to_eur(db_session, product_factory):
    _set_fx(db_session, {"USD": 1, "EUR": 1.08, "PLN": 0.25})
    product = product_factory(stock=10)
    _order(db_session, product=product, gross=1000, country="DE", currency="EUR")
    _order(db_session, product=product, gross=1000, country="DE", currency="USD")
    status = tax_threshold.eu_distance_selling_status(db_session)
    # 1000 EUR (identity) + 1000 USD * 1/1.08 = 1000 + 925.93.
    assert status.current.total_eur == Decimal("1925.93")
    assert status.current.unconvertible_count == 0


def test_unconvertible_currency_surfaced_not_dropped(db_session, product_factory):
    _set_fx(db_session, {"USD": 1, "EUR": 1.08})  # no PLN rate configured
    product = product_factory(stock=10)
    _order(db_session, product=product, gross=1000, country="DE", currency="EUR")
    _order(db_session, product=product, gross=5000, country="DE", currency="PLN")
    status = tax_threshold.eu_distance_selling_status(db_session)
    assert status.current.total_eur == Decimal("1000.00")  # PLN order excluded from sum
    assert status.current.unconvertible_count == 1  # but surfaced, not silent


# ---------------------------------------------------------------------------
# Status bands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gross", "expected"),
    [
        ("7999.99", tax_threshold.STATUS_NOT_APPROACHING),
        ("8000.00", tax_threshold.STATUS_APPROACHING),
        ("9999.99", tax_threshold.STATUS_APPROACHING),
        ("10000.00", tax_threshold.STATUS_THRESHOLD_MET),
        ("12000.00", tax_threshold.STATUS_THRESHOLD_MET),
    ],
)
def test_status_bands(db_session, product_factory, gross, expected):
    product = product_factory(stock=10)
    _order(db_session, product=product, gross=gross, country="DE")
    status = tax_threshold.eu_distance_selling_status(db_session)
    assert status.current.status == expected


def test_remaining_and_pct(db_session, product_factory):
    product = product_factory(stock=10)
    _order(db_session, product=product, gross="2500.00", country="DE")
    status = tax_threshold.eu_distance_selling_status(db_session)
    assert status.current.pct == Decimal("0.25")
    assert status.current.remaining_eur == Decimal("7500.00")


def test_remaining_floors_at_zero_once_met(db_session, product_factory):
    product = product_factory(stock=10)
    _order(db_session, product=product, gross="15000.00", country="DE")
    status = tax_threshold.eu_distance_selling_status(db_session)
    assert status.current.remaining_eur == Decimal("0.00")


# ---------------------------------------------------------------------------
# Current-or-prior-year liability
# ---------------------------------------------------------------------------


def test_prior_year_crossing_makes_liable(db_session, product_factory):
    """A prior-year crossing keeps the seller liable even if this year is quiet."""
    product = product_factory(stock=10)
    _order(db_session, product=product, gross="10000.00", country="DE", year=THIS_YEAR - 1)
    status = tax_threshold.eu_distance_selling_status(db_session)
    assert status.current.status == tax_threshold.STATUS_NOT_APPROACHING
    assert status.prior.status == tax_threshold.STATUS_THRESHOLD_MET
    assert status.liable is True
    assert status.effective_status == tax_threshold.STATUS_THRESHOLD_MET


def test_year_param_selects_window(db_session, product_factory):
    product = product_factory(stock=10)
    _order(db_session, product=product, gross="3000.00", country="DE", year=2024)
    status = tax_threshold.eu_distance_selling_status(db_session, year=2024)
    assert status.current.year == 2024
    assert status.current.total_eur == Decimal("3000.00")
    assert status.prior.year == 2023
    assert status.prior.total_eur == Decimal("0.00")
