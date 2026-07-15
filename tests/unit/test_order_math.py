"""Unit tests for the shared order/draft recompute (services.order_math)."""

from __future__ import annotations

from decimal import Decimal

from app.services.order_math import recompute_totals


def test_subtotal_sums_line_totals_exact():
    subtotal, total = recompute_totals(
        line_totals=[Decimal("10.10"), Decimal("2.05"), Decimal("0.01")],
        discount=Decimal("0"),
    )
    assert subtotal == Decimal("12.16")
    assert total == Decimal("12.16")


def test_none_line_totals_contribute_zero():
    subtotal, total = recompute_totals(
        line_totals=[Decimal("5.00"), None, Decimal("1.50")],
        discount=Decimal("0"),
    )
    assert subtotal == Decimal("6.50")
    assert total == Decimal("6.50")


def test_discount_and_shipping_applied():
    subtotal, total = recompute_totals(
        line_totals=[Decimal("100.00")],
        discount=Decimal("15.00"),
        shipping=Decimal("9.99"),
    )
    assert subtotal == Decimal("100.00")
    # 100 - 15 + 9.99
    assert total == Decimal("94.99")


def test_total_clamped_at_zero():
    subtotal, total = recompute_totals(
        line_totals=[Decimal("10.00")],
        discount=Decimal("50.00"),
    )
    assert subtotal == Decimal("10.00")
    assert total == Decimal("0.00")


def test_shipping_defaults_to_zero():
    subtotal, total = recompute_totals(
        line_totals=[Decimal("20.00")],
        discount=Decimal("5.00"),
    )
    assert total == Decimal("15.00")
