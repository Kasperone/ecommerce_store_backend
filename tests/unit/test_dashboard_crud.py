"""Unit tests for the dashboard CRUD aggregations (DB-backed, no HTTP).

Covers the revenue/order/customer counters, the zero-filled sales trend, top
products by units sold, the low-stock tile, and the `kpis` orchestrator. The
two invariants under test: revenue counts only ``REVENUE_STATUSES`` orders, and
money is reported per currency (never summed across currencies).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.crud import dashboard as crud_dashboard
from app.crud import inventory as crud_inventory
from app.crud import order as crud_order
from app.models.order import OrderStatus
from app.models.user import UserRole
from app.schemas.order import OrderCreate, OrderItemCreate

pytestmark = pytest.mark.integration


def _order_in(
    items: list[tuple[int, int]], *, shipping_method_id: int, currency: str = "USD"
) -> OrderCreate:
    return OrderCreate(
        items=[OrderItemCreate(product_id=pid, quantity=qty) for pid, qty in items],
        currency=currency,
        shipping_address="1 Test St",
        shipping_city="Testville",
        shipping_postal_code="00-001",
        shipping_country="PL",
        shipping_method_id=shipping_method_id,
    )


def _place(db, *, user_id, items, shipping_method_id, currency="USD", status=OrderStatus.PENDING):
    """Create an order and (if not pending) set its status, then commit."""
    order = crud_order.create_with_items(
        db,
        user_id=user_id,
        obj_in=_order_in(items, shipping_method_id=shipping_method_id, currency=currency),
    )
    if status != OrderStatus.PENDING:
        order.status = status
        db.add(order)
    db.commit()
    return order


def _naive_utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def test_revenue_stats_per_currency_excludes_non_revenue(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory()
    p_usd = product_factory(stock=100, price_usd=20.0)
    p_pln = product_factory(stock=100, price_pln=80.0)
    # USD: 40 (paid) + 20 (delivered) = 60 over 2 orders; PLN: 80 over 1 order.
    _place(
        db_session,
        user_id=customer.id,
        items=[(p_usd.id, 2)],
        status=OrderStatus.PAID,
        shipping_method_id=test_shipping_method_id,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(p_usd.id, 1)],
        status=OrderStatus.DELIVERED,
        shipping_method_id=test_shipping_method_id,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(p_pln.id, 1)],
        currency="PLN",
        status=OrderStatus.PAID,
        shipping_method_id=test_shipping_method_id,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(p_usd.id, 1)],
        status=OrderStatus.PENDING,
        shipping_method_id=test_shipping_method_id,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(p_usd.id, 1)],
        status=OrderStatus.CANCELLED,
        shipping_method_id=test_shipping_method_id,
    )

    stats = crud_dashboard.revenue_stats(db_session)
    assert stats["USD"] == {"revenue": 60.0, "orders": 2}
    assert stats["PLN"] == {"revenue": 80.0, "orders": 1}


def test_order_and_customer_counts(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory(email="c1@example.com")
    user_factory(email="c2@example.com")
    user_factory(email="admin@example.com", role=UserRole.ADMIN)
    product = product_factory(stock=100)
    _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 1)],
        status=OrderStatus.PENDING,
        shipping_method_id=test_shipping_method_id,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 1)],
        status=OrderStatus.PAID,
        shipping_method_id=test_shipping_method_id,
    )

    assert crud_dashboard.order_count(db_session) == 2
    assert crud_dashboard.order_count(db_session, status=OrderStatus.PENDING) == 1
    assert crud_dashboard.customer_count(db_session) == 2  # admin excluded

    future = _naive_utcnow() + timedelta(days=1)
    assert crud_dashboard.order_count(db_session, since=future) == 0
    assert crud_dashboard.customer_count(db_session, since=future) == 0


def test_sales_trend_zero_filled_volume_vs_revenue(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory()
    product = product_factory(stock=100, price_usd=20.0)
    _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 2)],
        status=OrderStatus.PAID,
        shipping_method_id=test_shipping_method_id,
    )  # 40
    _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 1)],
        status=OrderStatus.PENDING,
        shipping_method_id=test_shipping_method_id,
    )

    trend = crud_dashboard.sales_trend(db_session, days=7)
    today = _naive_utcnow().date()

    assert len(trend) == 7
    assert trend[0]["date"] == today - timedelta(days=6)
    assert trend[-1]["date"] == today
    # Today: both orders count toward volume, only the paid one toward revenue.
    assert trend[-1]["order_count"] == 2
    assert trend[-1]["revenue"] == {"USD": 40.0}
    # Earlier days are zero-filled.
    assert all(point["order_count"] == 0 for point in trend[:-1])
    assert trend[0]["revenue"] == {}


def test_sales_trend_explicit_range_bounds_both_ends(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    """An explicit start/end range attributes orders to the right day and bounds both ends."""
    from datetime import date as _date

    customer = user_factory()
    product = product_factory(stock=100, price_usd=20.0)
    order = _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 2)],
        status=OrderStatus.PAID,
        shipping_method_id=test_shipping_method_id,
    )
    # Backdate the order into the middle of a fixed historical range.
    order.created_at = datetime(2026, 1, 5, 12, 0, 0)
    db_session.add(order)
    db_session.commit()

    trend = crud_dashboard.sales_trend(
        db_session, start_date=_date(2026, 1, 1), end_date=_date(2026, 1, 10)
    )
    assert len(trend) == 10
    assert trend[0]["date"] == _date(2026, 1, 1)
    assert trend[-1]["date"] == _date(2026, 1, 10)
    day5 = next(p for p in trend if p["date"] == _date(2026, 1, 5))
    assert day5["order_count"] == 1
    assert day5["revenue"] == {"USD": 40.0}

    # A range that ends before the order's day excludes it entirely.
    earlier = crud_dashboard.sales_trend(
        db_session, start_date=_date(2026, 1, 1), end_date=_date(2026, 1, 4)
    )
    assert all(p["order_count"] == 0 for p in earlier)


def test_kpis_custom_range_windows_only_in_range(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    """`kpis` start/end bounds the windowed figures while leaving the all-time totals."""
    from datetime import date as _date

    customer = user_factory()
    product = product_factory(stock=100, price_usd=20.0)
    order = _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 2)],
        status=OrderStatus.PAID,
        shipping_method_id=test_shipping_method_id,
    )
    order.created_at = datetime(2026, 1, 5, 12, 0, 0)
    db_session.add(order)
    db_session.commit()

    payload = crud_dashboard.kpis(
        db_session, start_date=_date(2026, 1, 1), end_date=_date(2026, 1, 10)
    )
    assert payload["window_days"] == 10  # effective inclusive span, not the default 30
    assert len(payload["sales_trend"]) == 10
    assert payload["orders_window"] == 1
    assert payload["revenue_window"] == {"USD": 40.0}
    # All-time totals ignore the window.
    assert payload["orders_total"] == 1
    assert payload["revenue_total"] == {"USD": 40.0}

    # A range that doesn't cover the order zeroes the windowed figures but not the totals.
    outside = crud_dashboard.kpis(
        db_session, start_date=_date(2026, 2, 1), end_date=_date(2026, 2, 10)
    )
    assert outside["orders_window"] == 0
    assert outside["revenue_window"] == {}
    assert outside["orders_total"] == 1


def test_top_products_ranked_by_units_excludes_non_revenue(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory()
    p1 = product_factory(name="Widget", stock=100, price_usd=10.0)
    p2 = product_factory(name="Gadget", stock=200, price_usd=10.0)  # room for the qty-100 pending
    _place(
        db_session,
        user_id=customer.id,
        items=[(p1.id, 5)],
        status=OrderStatus.PAID,
        shipping_method_id=test_shipping_method_id,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(p1.id, 3)],
        status=OrderStatus.DELIVERED,
        shipping_method_id=test_shipping_method_id,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(p2.id, 2)],
        status=OrderStatus.PAID,
        shipping_method_id=test_shipping_method_id,
    )
    # Pending qty 100 must not count toward the ranking.
    _place(
        db_session,
        user_id=customer.id,
        items=[(p2.id, 100)],
        status=OrderStatus.PENDING,
        shipping_method_id=test_shipping_method_id,
    )

    top = crud_dashboard.top_products(db_session, limit=5)
    assert [t["product_id"] for t in top] == [p1.id, p2.id]
    assert top[0]["units_sold"] == 8  # 5 + 3 across two paid orders
    assert top[0]["order_count"] == 2  # distinct orders
    assert top[0]["name"] == "Widget"
    assert top[0]["slug"] == p1.slug
    assert top[1]["units_sold"] == 2  # pending qty 100 not counted


def test_low_stock_count_sample_and_status(db_session, product_factory):
    out0 = product_factory(name="Out", stock=0, allow_backorder=False, low_stock_threshold=None)
    back = product_factory(name="Back", stock=0, allow_backorder=True, low_stock_threshold=None)
    low2 = product_factory(name="Low", stock=2, low_stock_threshold=5)
    ok = product_factory(name="OK", stock=50, low_stock_threshold=5)

    count, sample = crud_dashboard.low_stock(db_session, limit=10)
    ids = {p.id for p in sample}

    assert count == 3
    assert ids == {out0.id, back.id, low2.id}
    assert ok.id not in ids
    # Ordered by stock asc — the stock=2 row comes last.
    assert sample[-1].id == low2.id
    assert crud_inventory.status_for(out0) == "out_of_stock"
    assert crud_inventory.status_for(back) == "backorder"
    assert crud_inventory.status_for(low2) == "low_stock"


def test_kpis_orchestrator_assembles_payload(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory()
    product = product_factory(stock=100, price_usd=20.0, price_pln=80.0)
    # 40 USD (paid) + 80 PLN (paid), plus one pending order.
    _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 2)],
        status=OrderStatus.PAID,
        shipping_method_id=test_shipping_method_id,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 1)],
        currency="PLN",
        status=OrderStatus.PAID,
        shipping_method_id=test_shipping_method_id,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 1)],
        status=OrderStatus.PENDING,
        shipping_method_id=test_shipping_method_id,
    )
    low = product_factory(name="LowOne", stock=1, low_stock_threshold=5)

    payload = crud_dashboard.kpis(db_session, window_days=14)

    assert payload["window_days"] == 14
    assert len(payload["sales_trend"]) == 14
    assert payload["orders_total"] == 3
    assert payload["orders_pending"] == 1
    assert payload["customers_total"] == 1
    assert payload["revenue_total"] == {"USD": 40.0, "PLN": 80.0}
    assert payload["primary_currency"] == "PLN"  # 80 > 40
    assert payload["aov_total"]["USD"] == 40.0
    assert payload["aov_total"]["PLN"] == 80.0
    assert payload["low_stock_count"] >= 1
    assert any(item["product_id"] == low.id for item in payload["low_stock"])
    assert payload["top_products"][0]["product_id"] == product.id
    assert isinstance(payload["generated_at"], datetime)
