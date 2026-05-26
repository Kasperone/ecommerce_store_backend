"""Unit coverage for ``CRUDProduct.get_multi_with_filters`` ordering.

The public product list grew ``sort_by`` / ``order`` params. The column is
resolved through the ``SORTABLE_COLUMNS`` whitelist (never interpolated), and a
nullable ``price_usd`` must sort last on ascending price so price-less products
never lead a "price low→high" view.
"""

from __future__ import annotations

from datetime import datetime

from app.crud import product as crud_product


def _ids(products):
    return [p.id for p in products]


def test_sort_by_name_asc_orders_alphabetically(db_session, product_factory):
    c = product_factory(name="Cucumber", slug="cucumber")
    a = product_factory(name="Apple", slug="apple")
    b = product_factory(name="Banana", slug="banana")

    result = crud_product.get_multi_with_filters(db_session, sort_by="name", order="asc")

    assert _ids(result) == [a.id, b.id, c.id]


def test_sort_by_name_desc_reverses(db_session, product_factory):
    a = product_factory(name="Apple", slug="apple")
    c = product_factory(name="Cucumber", slug="cucumber")
    b = product_factory(name="Banana", slug="banana")

    result = crud_product.get_multi_with_filters(db_session, sort_by="name", order="desc")

    assert _ids(result) == [c.id, b.id, a.id]


def test_sort_by_price_usd_asc(db_session, product_factory):
    mid = product_factory(slug="mid", price_usd=20.0)
    low = product_factory(slug="low", price_usd=10.0)
    high = product_factory(slug="high", price_usd=30.0)

    result = crud_product.get_multi_with_filters(db_session, sort_by="price_usd", order="asc")

    assert _ids(result) == [low.id, mid.id, high.id]


def test_sort_by_price_usd_desc(db_session, product_factory):
    low = product_factory(slug="low", price_usd=10.0)
    high = product_factory(slug="high", price_usd=30.0)
    mid = product_factory(slug="mid", price_usd=20.0)

    result = crud_product.get_multi_with_filters(db_session, sort_by="price_usd", order="desc")

    assert _ids(result) == [high.id, mid.id, low.id]


def test_price_usd_asc_puts_null_prices_last(db_session, product_factory):
    """A product with no USD price (PLN keeps the at-least-one-price CHECK happy)
    must trail the priced rows on ``price_usd asc`` thanks to NULLS LAST."""
    priced_low = product_factory(slug="priced-low", price_usd=10.0)
    no_usd = product_factory(slug="no-usd", price_usd=None, price_pln=50.0)
    priced_high = product_factory(slug="priced-high", price_usd=30.0)

    result = crud_product.get_multi_with_filters(db_session, sort_by="price_usd", order="asc")

    assert _ids(result) == [priced_low.id, priced_high.id, no_usd.id]


def test_default_sort_is_created_at_desc(db_session, product_factory):
    oldest = product_factory(slug="oldest", created_at=datetime(2026, 1, 1, 9, 0, 0))
    newest = product_factory(slug="newest", created_at=datetime(2026, 3, 1, 9, 0, 0))
    middle = product_factory(slug="middle", created_at=datetime(2026, 2, 1, 9, 0, 0))

    # No sort args → newest first.
    result = crud_product.get_multi_with_filters(db_session)

    assert _ids(result) == [newest.id, middle.id, oldest.id]


def test_unknown_sort_by_falls_back_to_created_at(db_session, product_factory):
    """The route's ``Literal`` blocks bad input at the HTTP edge, but the CRUD
    fallback keeps a stray internal caller from raising."""
    oldest = product_factory(slug="fb-oldest", created_at=datetime(2026, 1, 1, 9, 0, 0))
    newest = product_factory(slug="fb-newest", created_at=datetime(2026, 3, 1, 9, 0, 0))

    result = crud_product.get_multi_with_filters(db_session, sort_by="bogus", order="desc")

    assert _ids(result) == [newest.id, oldest.id]
