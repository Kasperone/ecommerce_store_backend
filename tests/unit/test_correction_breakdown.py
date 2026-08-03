"""Unit tests for the faktura-korygująca VAT breakdown (pure arithmetic, no PDF).

``build_corrective_invoice_pdf`` returns opaque bytes, so the PDF tests can only
assert "it rendered". That is exactly how this shipped wrong: the KOR decomposed
every correction at one flat ``store.vat_rate`` while the faktura used a per-rate
breakdown, and no test could see the difference.

``compute_correction_breakdown`` exists so the arithmetic is assertable on its
own. These tests pin the properties that make a correction legally valid:

- it reconciles against the faktura it corrects (same "before" figures);
- the correction sums to exactly the refunded amount;
- each rate's VAT is computed at THAT rate, not the store standard;
- reverse charge stays a zero-VAT net movement.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.order import Order, OrderItem, OrderStatus
from app.models.refund import Refund, RefundReason
from app.models.refund_item import RefundItem
from app.services.invoice import (
    StoreIdentity,
    _seller_lines,
    compute_correction_breakdown,
    compute_vat_breakdown,
    is_reverse_charge,
)
from app.services.tax import TREATMENT_REVERSE_CHARGE

pytestmark = pytest.mark.integration

D = Decimal


def _order(db_session, user, product, *lines, shipping="0.00", discount="0.00", treatment=None):
    """Build a persisted order from ``(unit_price, qty, vat_rate)`` triples."""
    items = [
        OrderItem(
            product_id=product.id,
            product_name=f"Line {i}",
            product_sku=f"SKU-{i}",
            quantity=qty,
            unit_price=D(price),
            total_price=D(price) * qty,
            vat_rate=rate,
        )
        for i, (price, qty, rate) in enumerate(lines)
    ]
    subtotal = sum((it.total_price for it in items), D("0.00"))
    order = Order(
        user_id=user.id,
        order_number=f"ORD-KORVAT-{user.id}-{len(lines)}-{shipping}-{discount}-{treatment}",
        status=OrderStatus.PAID,
        subtotal=subtotal,
        discount=D(discount),
        shipping=D(shipping),
        total=subtotal - D(discount) + D(shipping),
        currency="PLN",
        shipping_address="1 Test St",
        shipping_city="Warszawa",
        shipping_postal_code="00-001",
        shipping_country="PL",
        items=items,
    )
    if treatment is not None:
        order.tax_treatment = treatment
    db_session.add(order)
    db_session.flush()
    return order


def _refund(db_session, order, amount, *, picked=None):
    refund = Refund(
        order_id=order.id,
        amount=D(amount),
        currency=order.currency,
        reason=RefundReason.OTHER.value,
    )
    db_session.add(refund)
    db_session.flush()
    for order_item, qty in picked or []:
        db_session.add(RefundItem(refund_id=refund.id, order_item_id=order_item.id, quantity=qty))
    db_session.flush()
    db_session.refresh(refund)
    return refund


def test_before_side_equals_the_faktura_breakdown(db_session, user_factory, product_factory):
    """The correction's "before" column IS the original faktura's figures.

    This is the property that makes the document reconcile; if these two ever
    diverge the KOR is contradicting the invoice it corrects.
    """
    user = user_factory()
    product = product_factory()
    order = _order(db_session, user, product, ("100.00", 1, 23), ("50.00", 1, 5))
    refund = _refund(db_session, order, "30.00")

    faktura = compute_vat_breakdown(order, 23)
    correction = compute_correction_breakdown(order, refund, 23)

    assert [g.rate for g in correction] == [g.rate for g in faktura]
    for corr, orig in zip(correction, faktura, strict=True):
        assert corr.before == orig


def test_mixed_rate_full_refund_corrects_each_rate_at_its_own_rate(
    db_session, user_factory, product_factory
):
    """The bug this file exists for: a 5% line must be corrected at 5%.

    Under the old flat-rate decomposition the whole 150.00 was split at 23%,
    inventing ~2.14 PLN of VAT that never existed on the 5% line.
    """
    user = user_factory()
    product = product_factory()
    order = _order(db_session, user, product, ("123.00", 1, 23), ("105.00", 1, 5))
    refund = _refund(db_session, order, "228.00")  # full

    correction = compute_correction_breakdown(order, refund, 23)
    by_rate = {g.rate: g for g in correction}

    # 123.00 gross @23% → net 100.00, VAT 23.00
    assert by_rate[23].before.net == D("100.00")
    assert by_rate[23].before.vat == D("23.00")
    # 105.00 gross @5% → net 100.00, VAT 5.00
    assert by_rate[5].before.net == D("100.00")
    assert by_rate[5].before.vat == D("5.00")

    # Full refund zeroes every group.
    for group in correction:
        assert group.after.gross == D("0.00")
        assert group.after.vat == D("0.00")
        assert group.diff_gross == -group.before.gross
        assert group.diff_vat == -group.before.vat

    # A flat-23% decomposition would have reported this total instead.
    total_vat_corrected = sum(-g.diff_vat for g in correction)
    assert total_vat_corrected == D("28.00")
    assert total_vat_corrected != D("42.63")  # 228.00 decomposed wholly at 23%


def test_correction_sums_to_exactly_the_refunded_amount(db_session, user_factory, product_factory):
    """Rounding must never leak: the parts sum to refund.amount to the grosz."""
    user = user_factory()
    product = product_factory()
    order = _order(db_session, user, product, ("33.33", 1, 23), ("33.33", 1, 8), ("33.34", 1, 5))
    refund = _refund(db_session, order, "50.00")  # partial, awkward split

    correction = compute_correction_breakdown(order, refund, 23)
    corrected_gross = sum(-g.diff_gross for g in correction)
    assert corrected_gross == D("50.00")


def test_picked_lines_drive_the_rate_mix(db_session, user_factory, product_factory):
    """Refunding only the 5% line must correct 5% VAT and leave 23% untouched.

    Without the picker the amount would be spread pro-rata across both rates,
    correcting 23% VAT the customer is not getting back.
    """
    user = user_factory()
    product = product_factory()
    order = _order(db_session, user, product, ("123.00", 1, 23), ("105.00", 1, 5))
    reduced_line = next(it for it in order.items if it.vat_rate == 5)
    refund = _refund(db_session, order, "105.00", picked=[(reduced_line, 1)])

    by_rate = {g.rate: g for g in compute_correction_breakdown(order, refund, 23)}

    # The 5% group is fully corrected...
    assert by_rate[5].after.gross == D("0.00")
    assert by_rate[5].diff_vat == D("-5.00")
    # ...and the 23% group is untouched.
    assert by_rate[23].after.gross == by_rate[23].before.gross
    assert by_rate[23].diff_vat == D("0.00")


def test_amount_only_refund_falls_back_to_pro_rata(db_session, user_factory, product_factory):
    """A goodwill refund has no picker; spreading it across the order's own rate
    mix is the only defensible reading, and it must still sum exactly."""
    user = user_factory()
    product = product_factory()
    order = _order(db_session, user, product, ("100.00", 1, 23), ("100.00", 1, 8))
    refund = _refund(db_session, order, "100.00")

    correction = compute_correction_breakdown(order, refund, 23)
    assert sum(-g.diff_gross for g in correction) == D("100.00")
    # Equal group grosses → the refund splits evenly.
    assert {(-g.diff_gross) for g in correction} == {D("50.00")}


def test_reverse_charge_correction_has_no_vat(db_session, user_factory, product_factory):
    """A reverse-charged sale carries 0% lines; correcting it must not conjure
    VAT. The old flat decomposition applied the 23% store rate and did."""
    user = user_factory()
    product = product_factory()
    order = _order(db_session, user, product, ("100.00", 1, 0), treatment=TREATMENT_REVERSE_CHARGE)
    refund = _refund(db_session, order, "100.00")

    correction = compute_correction_breakdown(order, refund, 23)
    assert [g.rate for g in correction] == [0]
    group = correction[0]
    assert group.before.vat == D("0.00")
    assert group.after.vat == D("0.00")
    assert group.diff_vat == D("0.00")
    # The whole movement is net.
    assert group.diff_net == D("-100.00")


def test_shipping_joins_the_standard_rate_group(db_session, user_factory, product_factory):
    """Delivery is a standard-rate service in PL, so it lands in the store-rate
    group — and the correction has to account for it or the columns won't
    reconcile with the faktura's total."""
    user = user_factory()
    product = product_factory()
    order = _order(db_session, user, product, ("100.00", 1, 8), shipping="23.00")
    refund = _refund(db_session, order, "123.00")

    by_rate = {g.rate: g for g in compute_correction_breakdown(order, refund, 23)}
    assert by_rate[23].before.gross == D("23.00")  # shipping only
    assert by_rate[8].before.gross == D("100.00")
    assert sum(-g.diff_gross for g in by_rate.values()) == D("123.00")


# ---------------------------------------------------------------------------
# shared party block + reverse-charge predicate
#
# The seller block was copy-pasted between the two renderers and the copy in the
# corrective one had drifted. These test the now-shared helper directly, which
# is the only assertion-friendly surface (PDF body text is inside a FlateDecode
# stream, so it is not byte-searchable).
# ---------------------------------------------------------------------------


def _labels():
    from app.services.invoice import _LABELS

    return _LABELS["en"]


def test_seller_block_prints_the_eu_vat_id_under_reverse_charge(
    db_session, user_factory, product_factory
):
    user = user_factory()
    product = product_factory()
    order = _order(db_session, user, product, ("100.00", 1, 0), treatment=TREATMENT_REVERSE_CHARGE)
    store = StoreIdentity(name="E-shop", tax_id="5252445724", vat_id="PL5252445724")

    lines = _seller_lines(order, store, _labels())

    assert any("PL5252445724" in line for line in lines)


def test_seller_block_omits_the_vat_id_on_a_domestic_sale(
    db_session, user_factory, product_factory
):
    """The positive control for the test above — without this, a helper that
    printed the VAT-ID unconditionally would pass and prove nothing."""
    user = user_factory()
    product = product_factory()
    order = _order(db_session, user, product, ("123.00", 1, 23))
    store = StoreIdentity(name="E-shop", tax_id="5252445724", vat_id="PL5252445724")

    lines = _seller_lines(order, store, _labels())

    assert not any("PL5252445724" in line for line in lines)
    assert any("5252445724" in line for line in lines)  # the bare NIP still prints


def test_seller_block_falls_back_to_the_nip_when_no_vat_id_configured(
    db_session, user_factory, product_factory
):
    """`STORE_VAT_ID` is blank by default, so the fallback is the common case
    until it is set in production."""
    user = user_factory()
    product = product_factory()
    order = _order(db_session, user, product, ("100.00", 1, 0), treatment=TREATMENT_REVERSE_CHARGE)
    store = StoreIdentity(name="E-shop", tax_id="5252445724", vat_id="")

    lines = _seller_lines(order, store, _labels())
    vat_id_label = _labels()["vat_id"]

    assert any(line.startswith(f"{vat_id_label}:") and "5252445724" in line for line in lines)


def test_is_reverse_charge_predicate(db_session, user_factory, product_factory):
    user = user_factory()
    product = product_factory()
    rc = _order(db_session, user, product, ("100.00", 1, 0), treatment=TREATMENT_REVERSE_CHARGE)
    domestic = _order(db_session, user, product, ("123.00", 1, 23))

    assert is_reverse_charge(rc) is True
    assert is_reverse_charge(domestic) is False
