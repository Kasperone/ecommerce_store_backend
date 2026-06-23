"""Unit tests for the invoice PDF service (pure functions, no DB, no HTTP).

Orders are constructed as transient ORM instances so ``build_invoice_pdf`` is
exercised in isolation from the route/session layer.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.models.order import Order, OrderItem, OrderStatus
from app.models.user import User
from app.services.invoice import (
    StoreIdentity,
    build_invoice_pdf,
    compute_vat_breakdown,
    decompose_gross,
    derive_proforma_number,
)

STORE = StoreIdentity(
    name="E-commerce Store",
    address_line="ul. Sklepowa 1",
    postal_city="00-123 Warszawa",
    country="Polska",
    tax_id="PL9876543210",
    email="shop@example.com",
    vat_rate=23,
)


def _order(user: User, *, currency: str = "PLN") -> Order:
    order = Order(
        order_number="ORD-20260527-AB12CD",
        status=OrderStatus.PAID,
        subtotal=199.98,
        tax=0.0,
        shipping=15.0,
        total=214.98,
        currency=currency,
        shipping_address="ul. Polna 7/3",
        shipping_city="Kraków",
        shipping_postal_code="30-001",
        shipping_country="Polska",
        payment_method="stripe",
        created_at=dt.datetime(2026, 5, 27, 10, 30),
    )
    order.user = user
    # Diacritics + the three markup-sensitive chars (& < >) must round-trip.
    order.items = [
        OrderItem(
            product_name="Słuchawki «Ω»",
            product_sku="SKU-Ł-01",
            quantity=2,
            unit_price=79.99,
            total_price=159.98,
        ),
        OrderItem(
            product_name="Etui & kabel <USB-C>",
            product_sku=None,
            quantity=1,
            unit_price=55.0,
            total_price=55.0,
        ),
    ]
    return order


def _company_user() -> User:
    return User(
        email="anna.kowalska@example.com",
        first_name="Anna",
        last_name="Kowalska",
        company_name="Kowalska Sp. z o.o.",
        company_tax_id="PL1234567890",
        company_address_street="ul. Kwiatowa 5",
        company_address_postal_code="00-001",
        company_address_city="Warszawa",
        company_address_country="Polska",
    )


def _person_user() -> User:
    return User(email="john@example.com", first_name="John", last_name="Doe")


def _vat_order(items, *, discount=0.0, shipping=0.0, total) -> Order:
    """Minimal order carrying line items with explicit vat_rate (for breakdown)."""
    order = Order(
        order_number="ORD-VAT",
        status=OrderStatus.PAID,
        subtotal=sum(tp for _, tp, _ in items),
        tax=0.0,
        shipping=shipping,
        discount=discount,
        total=total,
        currency="PLN",
        shipping_address="ul. Polna 7/3",
        shipping_city="Kraków",
        shipping_postal_code="30-001",
        shipping_country="Polska",
    )
    order.items = [
        OrderItem(product_name=name, quantity=1, unit_price=tp, total_price=tp, vat_rate=rate)
        for name, tp, rate in items
    ]
    return order


class TestComputeVatBreakdown:
    def test_mixed_rates_groups_and_reconciles_to_total(self):
        # 100 @ 23% goods + 50 @ 5% goods + 10 shipping (standard 23%) = 160 gross.
        order = _vat_order([("std", 100.0, 23), ("book", 50.0, 5)], shipping=10.0, total=160.0)
        groups = compute_vat_breakdown(order, shipping_vat_rate=23)
        # Sorted by rate desc; shipping folds into the 23% group.
        assert [g.rate for g in groups] == [23, 5]
        assert {g.rate: g.gross for g in groups} == {23: Decimal("110.00"), 5: Decimal("50.00")}
        # Each group reconciles net + VAT == gross, and grosses sum to the total.
        for g in groups:
            assert g.net + g.vat == g.gross
        assert sum(g.gross for g in groups) == order.total

    def test_discount_allocated_proportionally_last_group_absorbs_remainder(self):
        # 100 @ 23% + 100 @ 5%, 20 discount, no shipping → each group loses 10.
        order = _vat_order([("std", 100.0, 23), ("book", 100.0, 5)], discount=20.0, total=180.0)
        groups = compute_vat_breakdown(order, shipping_vat_rate=23)
        assert {g.rate: g.gross for g in groups} == {23: Decimal("90.00"), 5: Decimal("90.00")}
        assert sum(g.gross for g in groups) == order.total

    def test_single_rate_matches_whole_order_decomposition(self):
        # One rate → the single group's gross is the whole total, identical to
        # decompose_gross(total, rate).
        order = _vat_order([("a", 200.0, 23)], shipping=14.98, total=214.98)
        groups = compute_vat_breakdown(order, shipping_vat_rate=23)
        assert len(groups) == 1
        net, vat = decompose_gross(Decimal("214.98"), 23)
        assert (groups[0].net, groups[0].vat) == (net, vat)


# --- decompose_gross -------------------------------------------------------


def test_decompose_gross_23pct():
    net, vat = decompose_gross(123.00, 23)
    assert net == 100.00
    assert vat == 23.00
    assert round(net + vat, 2) == 123.00


def test_decompose_gross_rounding_edge():
    # 100 gross @ 23% → 81.30 net + 18.70 VAT, summing back exactly.
    net, vat = decompose_gross(100.00, 23)
    assert net == Decimal("81.30")
    assert vat == Decimal("18.70")
    assert net + vat == Decimal("100.00")


def test_decompose_gross_zero_rate():
    assert decompose_gross(100.0, 0) == (100.0, 0.0)


def test_decompose_gross_negative_rate_treated_as_zero():
    assert decompose_gross(100.0, -5) == (100.0, 0.0)


# --- derive_proforma_number ------------------------------------------------


def test_derive_proforma_number_replaces_ord_prefix():
    assert derive_proforma_number(_order(_person_user())) == "PF-20260527-AB12CD"


def test_derive_proforma_number_without_ord_prefix():
    order = _order(_person_user())
    order.order_number = "X-999"
    assert derive_proforma_number(order) == "PF-X-999"


# --- build_invoice_pdf -----------------------------------------------------

NUMBER = "FV/2026/00001"
ISSUED = dt.date(2026, 5, 27)


def test_build_pdf_company_buyer_returns_pdf_bytes():
    pdf = build_invoice_pdf(
        _order(_company_user()), store=STORE, invoice_number=NUMBER, issued_at=ISSUED, lang="en"
    )
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 1000


def test_build_pdf_person_buyer():
    pdf = build_invoice_pdf(
        _order(_person_user()), store=STORE, invoice_number=NUMBER, issued_at=ISSUED, lang="en"
    )
    assert pdf[:4] == b"%PDF"


def test_build_pdf_guest_buyer():
    # Guest orders have no user — the buyer block bills to the guest email.
    order = _order(_person_user())
    order.user = None
    order.user_id = None
    order.guest_email = "guest@example.com"
    pdf = build_invoice_pdf(order, store=STORE, invoice_number=NUMBER, issued_at=ISSUED, lang="en")
    assert pdf[:4] == b"%PDF"


def test_build_pdf_polish_with_diacritics_does_not_raise():
    pdf = build_invoice_pdf(
        _order(_company_user()), store=STORE, invoice_number=NUMBER, issued_at=ISSUED, lang="pl"
    )
    assert pdf[:4] == b"%PDF"


def test_build_pdf_zero_vat_receipt():
    pdf = build_invoice_pdf(
        _order(_person_user()),
        store=StoreIdentity(name="Shop", vat_rate=0),
        invoice_number=NUMBER,
        issued_at=ISSUED,
    )
    assert pdf[:4] == b"%PDF"


def test_build_pdf_unknown_lang_falls_back_to_en():
    pdf = build_invoice_pdf(
        _order(_person_user()), store=STORE, invoice_number=NUMBER, issued_at=ISSUED, lang="zz"
    )
    assert pdf[:4] == b"%PDF"


def test_build_pdf_proforma_mode_en_and_pl():
    # The proforma label path (new keys) must render valid PDF bytes in both locales.
    order = _order(_person_user())
    ref = derive_proforma_number(order)
    for lang in ("en", "pl"):
        pdf = build_invoice_pdf(
            order, store=STORE, invoice_number=ref, issued_at=ISSUED, is_proforma=True, lang=lang
        )
        assert pdf[:4] == b"%PDF"


def test_build_pdf_variant_line_appends_title():
    """A variant line renders ``name — variant_title`` (and must not crash on
    the markup-escaped path)."""
    order = _order(_person_user())
    order.items.append(
        OrderItem(
            product_name="Koszulka <Premium>",
            variant_title="Czerwony / XL",
            product_sku="TS-RED-XL",
            quantity=1,
            unit_price=49.99,
            total_price=49.99,
        )
    )
    for lang in ("en", "pl"):
        pdf = build_invoice_pdf(
            order, store=STORE, invoice_number=NUMBER, issued_at=ISSUED, lang=lang
        )
        assert pdf[:4] == b"%PDF"
