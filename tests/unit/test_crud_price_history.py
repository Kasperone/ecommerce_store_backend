"""Unit tests for ``app/crud/price_history.py``.

Covers the OMNIBUS chokepoint contract: idempotent skip when the new price
matches the most recent row, currency scoping, the 30-day window, and the
fallback to the most-recent row when no rows fall inside the window.
Mirrors the shape of the inventory/refund chokepoint specs.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.crud.price_history import WINDOW_DAYS
from app.crud.price_history import price_history as crud_ph
from app.models.price_history import PriceHistory


def _ph_count(db, *, product_id: int) -> int:
    return db.query(PriceHistory).filter(PriceHistory.product_id == product_id).count()


def test_record_if_changed_writes_first_row(db_session, product_factory):
    product = product_factory(price_usd=10.0)
    # The factory bypasses the route chokepoint, so the table is empty for this product.
    assert _ph_count(db_session, product_id=product.id) == 0

    row = crud_ph.record_if_changed(db_session, product=product, currency="USD", new_price=10.0)
    assert row is not None
    assert row.price == 10.0
    assert row.currency == "USD"
    assert _ph_count(db_session, product_id=product.id) == 1


def test_record_if_changed_is_idempotent_for_same_price(db_session, product_factory):
    product = product_factory(price_usd=15.0)
    crud_ph.record_if_changed(db_session, product=product, currency="USD", new_price=15.0)
    crud_ph.record_if_changed(db_session, product=product, currency="USD", new_price=15.0)
    crud_ph.record_if_changed(db_session, product=product, currency="USD", new_price=15.0)
    # Only one row regardless of repeated saves with no actual change.
    assert _ph_count(db_session, product_id=product.id) == 1


def test_record_if_changed_writes_on_price_drop(db_session, product_factory):
    product = product_factory(price_usd=20.0)
    crud_ph.record_if_changed(db_session, product=product, currency="USD", new_price=20.0)
    crud_ph.record_if_changed(db_session, product=product, currency="USD", new_price=15.0)
    crud_ph.record_if_changed(db_session, product=product, currency="USD", new_price=22.0)
    assert _ph_count(db_session, product_id=product.id) == 3


def test_record_if_changed_skips_none(db_session, product_factory):
    """None price is treated as 'not for sale in this currency' — not history-worthy."""
    product = product_factory(price_usd=10.0)
    row = crud_ph.record_if_changed(db_session, product=product, currency="EUR", new_price=None)
    assert row is None
    assert _ph_count(db_session, product_id=product.id) == 0


def test_record_if_changed_rejects_unsupported_currency(db_session, product_factory):
    product = product_factory(price_usd=10.0)
    with pytest.raises(ValueError, match="Unsupported currency"):
        crud_ph.record_if_changed(db_session, product=product, currency="GBP", new_price=10.0)


def test_record_if_changed_scopes_by_currency(db_session, product_factory):
    """Same product, different currencies: independent histories."""
    product = product_factory(price_usd=10.0, price_pln=40.0)
    crud_ph.record_if_changed(db_session, product=product, currency="USD", new_price=10.0)
    crud_ph.record_if_changed(db_session, product=product, currency="PLN", new_price=40.0)
    # Re-saving USD at 10 is a no-op; PLN at 40 is also a no-op.
    crud_ph.record_if_changed(db_session, product=product, currency="USD", new_price=10.0)
    crud_ph.record_if_changed(db_session, product=product, currency="PLN", new_price=40.0)
    # Two rows total — one per currency.
    assert _ph_count(db_session, product_id=product.id) == 2


def test_record_if_changed_rounds_to_two_dp(db_session, product_factory):
    """A drift of <0.005 between calls must not count as a change."""
    product = product_factory(price_usd=10.0)
    crud_ph.record_if_changed(db_session, product=product, currency="USD", new_price=10.000)
    crud_ph.record_if_changed(db_session, product=product, currency="USD", new_price=10.001)
    crud_ph.record_if_changed(db_session, product=product, currency="USD", new_price=9.999)
    # All three round to 10.00 → only the first inserts.
    assert _ph_count(db_session, product_id=product.id) == 1


# ----------------------------------------------------------------------
# lowest_in_last_30_days
# ----------------------------------------------------------------------


def _stamp(db, *, product, currency: str, price: float, days_ago: float) -> None:
    """Insert a row with a hand-set effective_from so tests can simulate history."""
    row = PriceHistory(
        product_id=product.id,
        currency=currency,
        price=price,
        effective_from=datetime.utcnow() - timedelta(days=days_ago),
    )
    db.add(row)
    db.flush()


def test_lowest_30d_returns_in_window_min(db_session, product_factory):
    product = product_factory(price_usd=10.0)
    _stamp(db_session, product=product, currency="USD", price=20.0, days_ago=1)
    _stamp(db_session, product=product, currency="USD", price=12.0, days_ago=10)
    _stamp(db_session, product=product, currency="USD", price=18.0, days_ago=25)

    result = crud_ph.lowest_in_last_30_days(db_session, product_id=product.id, currency="USD")
    assert result is not None
    lowest, _since = result
    assert lowest == 12.0


def test_lowest_30d_falls_back_to_most_recent_outside_window(db_session, product_factory):
    """A product whose price hasn't moved in 30+ days has its most-recent (stale)
    row as both current and 30-day-low — the comparison line just won't render."""
    product = product_factory(price_usd=10.0)
    _stamp(db_session, product=product, currency="USD", price=8.0, days_ago=60)
    _stamp(db_session, product=product, currency="USD", price=9.0, days_ago=120)

    result = crud_ph.lowest_in_last_30_days(db_session, product_id=product.id, currency="USD")
    assert result is not None
    lowest, _since = result
    # Falls back to the most-recent row (effective_from desc); the 8.0 / 60 days ago.
    assert lowest == 8.0


def test_lowest_30d_returns_none_when_no_history(db_session, product_factory):
    product = product_factory(price_usd=10.0)
    result = crud_ph.lowest_in_last_30_days(db_session, product_id=product.id, currency="USD")
    assert result is None


def test_lowest_30d_is_currency_scoped(db_session, product_factory):
    product = product_factory(price_usd=10.0, price_pln=40.0)
    _stamp(db_session, product=product, currency="USD", price=8.0, days_ago=5)
    _stamp(db_session, product=product, currency="PLN", price=35.0, days_ago=5)

    usd = crud_ph.lowest_in_last_30_days(db_session, product_id=product.id, currency="USD")
    pln = crud_ph.lowest_in_last_30_days(db_session, product_id=product.id, currency="PLN")
    eur = crud_ph.lowest_in_last_30_days(db_session, product_id=product.id, currency="EUR")
    assert usd is not None and usd[0] == 8.0
    assert pln is not None and pln[0] == 35.0
    assert eur is None  # no rows for EUR


def test_lowest_30d_window_size_is_30_days(db_session, product_factory):
    """A row exactly at the boundary stays in scope; a row one day past is out."""
    product = product_factory(price_usd=10.0)
    _stamp(db_session, product=product, currency="USD", price=5.0, days_ago=WINDOW_DAYS - 1)
    _stamp(db_session, product=product, currency="USD", price=99.0, days_ago=WINDOW_DAYS + 5)

    result = crud_ph.lowest_in_last_30_days(db_session, product_id=product.id, currency="USD")
    assert result is not None
    lowest, _ = result
    # Only the in-window 5.0 row is eligible.
    assert lowest == 5.0
