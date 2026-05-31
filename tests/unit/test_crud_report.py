"""Unit tests for the report CRUD aggregations (DB-backed, no HTTP).

Covers the four read-only aggregates — sales-over-time (day/week/month buckets,
zero-fill, per-currency), top-products (units ranking + per-currency revenue),
revenue-by-category (current-category attribution + Uncategorized bucket), and
refund-summary (per-currency refunded/revenue/rate). The two invariants under
test mirror the dashboard: revenue counts only ``REVENUE_STATUSES`` orders, and
money is reported per currency (never summed across currencies). Date-range
filtering (UTC-day inclusive) is exercised by backdating ``created_at``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.crud import order as crud_order
from app.crud import report as crud_report
from app.models.order import Order, OrderStatus
from app.models.refund import Refund
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


def _place(
    db,
    *,
    user_id,
    items,
    shipping_method_id,
    currency="USD",
    status=OrderStatus.PAID,
    created_at: datetime | None = None,
) -> Order:
    """Create an order, set status + optional backdated ``created_at``, commit."""
    order = crud_order.create_with_items(
        db,
        user_id=user_id,
        obj_in=_order_in(items, shipping_method_id=shipping_method_id, currency=currency),
    )
    order.status = status
    if created_at is not None:
        order.created_at = created_at
    db.add(order)
    db.commit()
    return order


def _naive_utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Sales over time
# ---------------------------------------------------------------------------


def test_sales_over_time_zero_filled_daily_per_currency(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory()
    p_usd = product_factory(stock=100, price_usd=20.0)
    p_pln = product_factory(stock=100, price_pln=80.0)
    today = _naive_utcnow().date()

    # Two paid orders today (USD 40 + PLN 80), one paid order 2 days ago (USD 20),
    # one PENDING today (must not count toward revenue or the order count).
    _place(
        db_session,
        user_id=customer.id,
        items=[(p_usd.id, 2)],
        shipping_method_id=test_shipping_method_id,
        status=OrderStatus.PAID,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(p_pln.id, 1)],
        currency="PLN",
        shipping_method_id=test_shipping_method_id,
        status=OrderStatus.PAID,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(p_usd.id, 1)],
        shipping_method_id=test_shipping_method_id,
        status=OrderStatus.PAID,
        created_at=datetime.combine(today - timedelta(days=2), datetime.min.time())
        + timedelta(hours=12),
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(p_usd.id, 1)],
        shipping_method_id=test_shipping_method_id,
        status=OrderStatus.PENDING,
    )

    buckets = crud_report.sales_over_time(
        db_session, start=today - timedelta(days=4), end=today, granularity="day"
    )

    # 5 days inclusive, zero-filled.
    assert len(buckets) == 5
    assert buckets[0]["bucket"] == today - timedelta(days=4)
    assert buckets[-1]["bucket"] == today

    by_day = {b["bucket"]: b for b in buckets}
    # Today: 2 revenue orders, USD 40 + PLN 80 (per-currency, never summed).
    assert by_day[today]["orders"] == 2
    assert by_day[today]["revenue"] == {"USD": 40.0, "PLN": 80.0}
    # 2 days ago: 1 revenue order, USD 20.
    assert by_day[today - timedelta(days=2)]["orders"] == 1
    assert by_day[today - timedelta(days=2)]["revenue"] == {"USD": 20.0}
    # The in-between zero day.
    assert by_day[today - timedelta(days=1)] == {
        "bucket": today - timedelta(days=1),
        "orders": 0,
        "revenue": {},
    }


def test_sales_over_time_month_bucketing(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory()
    product = product_factory(stock=100, price_usd=10.0)
    # One order in Jan, two in Feb (fixed historical dates so the bucket math is
    # deterministic regardless of "today").
    _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 1)],
        shipping_method_id=test_shipping_method_id,
        created_at=datetime(2025, 1, 15, 10, 0, 0),
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 2)],
        shipping_method_id=test_shipping_method_id,
        created_at=datetime(2025, 2, 3, 10, 0, 0),
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 3)],
        shipping_method_id=test_shipping_method_id,
        created_at=datetime(2025, 2, 20, 10, 0, 0),
    )

    buckets = crud_report.sales_over_time(
        db_session, start=date(2025, 1, 10), end=date(2025, 3, 5), granularity="month"
    )
    by_bucket = {b["bucket"]: b for b in buckets}

    # Buckets keyed by the 1st of each month, zero-filled across Jan/Feb/Mar.
    assert set(by_bucket) == {date(2025, 1, 1), date(2025, 2, 1), date(2025, 3, 1)}
    assert by_bucket[date(2025, 1, 1)]["orders"] == 1
    assert by_bucket[date(2025, 1, 1)]["revenue"] == {"USD": 10.0}
    assert by_bucket[date(2025, 2, 1)]["orders"] == 2
    assert by_bucket[date(2025, 2, 1)]["revenue"] == {"USD": 50.0}  # 20 + 30
    assert by_bucket[date(2025, 3, 1)] == {
        "bucket": date(2025, 3, 1),
        "orders": 0,
        "revenue": {},
    }


def test_sales_over_time_week_bucketing_monday_start(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory()
    product = product_factory(stock=100, price_usd=10.0)
    # 2025-01-15 is a Wednesday → its ISO week starts Monday 2025-01-13.
    _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 1)],
        shipping_method_id=test_shipping_method_id,
        created_at=datetime(2025, 1, 15, 10, 0, 0),
    )

    buckets = crud_report.sales_over_time(
        db_session, start=date(2025, 1, 13), end=date(2025, 1, 19), granularity="week"
    )
    assert len(buckets) == 1
    assert buckets[0]["bucket"] == date(2025, 1, 13)  # Monday
    assert buckets[0]["orders"] == 1


def test_sales_over_time_date_range_excludes_outside(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory()
    product = product_factory(stock=100, price_usd=10.0)
    # In range (2025-02-10) and just outside the inclusive end (2025-02-16).
    _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 1)],
        shipping_method_id=test_shipping_method_id,
        created_at=datetime(2025, 2, 10, 12, 0, 0),
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 5)],
        shipping_method_id=test_shipping_method_id,
        created_at=datetime(2025, 2, 16, 12, 0, 0),
    )

    buckets = crud_report.sales_over_time(
        db_session, start=date(2025, 2, 10), end=date(2025, 2, 15), granularity="day"
    )
    total_orders = sum(b["orders"] for b in buckets)
    # Only the 2025-02-10 order is in range; the 2025-02-16 one is past `end`.
    assert total_orders == 1


# ---------------------------------------------------------------------------
# Top products
# ---------------------------------------------------------------------------


def test_top_products_ranked_by_units_with_per_currency_revenue(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory()
    p1 = product_factory(name="Widget", stock=200, price_usd=10.0, price_pln=40.0)
    p2 = product_factory(name="Gadget", stock=200, price_usd=10.0)
    # p1: 5 (USD, paid) + 3 (PLN, delivered) = 8 units, USD 50 + PLN 120.
    _place(
        db_session,
        user_id=customer.id,
        items=[(p1.id, 5)],
        shipping_method_id=test_shipping_method_id,
        status=OrderStatus.PAID,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(p1.id, 3)],
        currency="PLN",
        shipping_method_id=test_shipping_method_id,
        status=OrderStatus.DELIVERED,
    )
    # p2: 2 units paid.
    _place(
        db_session,
        user_id=customer.id,
        items=[(p2.id, 2)],
        shipping_method_id=test_shipping_method_id,
        status=OrderStatus.PAID,
    )
    # Pending qty 100 on p2 must NOT count.
    _place(
        db_session,
        user_id=customer.id,
        items=[(p2.id, 100)],
        shipping_method_id=test_shipping_method_id,
        status=OrderStatus.PENDING,
    )

    rows = crud_report.top_products(db_session, limit=10)
    assert [r["product_id"] for r in rows] == [p1.id, p2.id]
    assert rows[0]["units"] == 8
    assert rows[0]["order_count"] == 2
    assert rows[0]["name"] == "Widget"
    assert rows[0]["slug"] == p1.slug
    # Per-currency revenue, never summed.
    assert rows[0]["revenue_by_currency"] == {"USD": 50.0, "PLN": 120.0}
    assert rows[1]["units"] == 2  # pending qty 100 excluded
    assert rows[1]["revenue_by_currency"] == {"USD": 20.0}


def test_top_products_limit_and_date_range(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory()
    p1 = product_factory(name="A", stock=100, price_usd=10.0)
    p2 = product_factory(name="B", stock=100, price_usd=10.0)
    # p1 sold inside the window, p2 outside it.
    _place(
        db_session,
        user_id=customer.id,
        items=[(p1.id, 3)],
        shipping_method_id=test_shipping_method_id,
        created_at=datetime(2025, 4, 10, 12, 0, 0),
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(p2.id, 9)],
        shipping_method_id=test_shipping_method_id,
        created_at=datetime(2025, 1, 1, 12, 0, 0),
    )

    rows = crud_report.top_products(
        db_session, start=date(2025, 4, 1), end=date(2025, 4, 30), limit=10
    )
    assert [r["product_id"] for r in rows] == [p1.id]

    # limit honored.
    rows_top1 = crud_report.top_products(db_session, limit=1)
    assert len(rows_top1) == 1


# ---------------------------------------------------------------------------
# Revenue by category
# ---------------------------------------------------------------------------


def test_revenue_by_category_attribution_and_uncategorized(
    db_session, user_factory, product_factory, category_factory, test_shipping_method_id
):
    customer = user_factory()
    cat_a = category_factory(name="Alpha")
    cat_b = category_factory(name="Beta")
    p_a = product_factory(name="A1", stock=100, price_usd=10.0, category=cat_a)
    p_b = product_factory(name="B1", stock=100, price_usd=10.0, category=cat_b)
    p_none = product_factory(name="N1", stock=100, price_usd=10.0, category=None)

    # cat_a: 5 units / USD 50; cat_b: 2 units / USD 20; none: 3 units / USD 30.
    _place(
        db_session,
        user_id=customer.id,
        items=[(p_a.id, 5)],
        shipping_method_id=test_shipping_method_id,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(p_b.id, 2)],
        shipping_method_id=test_shipping_method_id,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(p_none.id, 3)],
        shipping_method_id=test_shipping_method_id,
    )

    rows = crud_report.revenue_by_category(db_session)
    by_id = {r["category_id"]: r for r in rows}

    assert by_id[cat_a.id]["name"] == "Alpha"
    assert by_id[cat_a.id]["units"] == 5
    assert by_id[cat_a.id]["revenue_by_currency"] == {"USD": 50.0}
    assert by_id[cat_b.id]["units"] == 2
    assert by_id[None]["name"] == "Uncategorized"
    assert by_id[None]["units"] == 3
    assert by_id[None]["revenue_by_currency"] == {"USD": 30.0}

    # Ranked by units desc: cat_a (5) > None (3) > cat_b (2).
    assert [r["category_id"] for r in rows] == [cat_a.id, None, cat_b.id]


def test_revenue_by_category_uncategorized_last_on_tie(
    db_session, user_factory, product_factory, category_factory, test_shipping_method_id
):
    customer = user_factory()
    cat = category_factory(name="Tied")
    p_cat = product_factory(name="C", stock=100, price_usd=10.0, category=cat)
    p_none = product_factory(name="N", stock=100, price_usd=10.0, category=None)
    # Equal units (3 each) → the uncategorized bucket sorts last on the tie.
    _place(
        db_session,
        user_id=customer.id,
        items=[(p_cat.id, 3)],
        shipping_method_id=test_shipping_method_id,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(p_none.id, 3)],
        shipping_method_id=test_shipping_method_id,
    )

    rows = crud_report.revenue_by_category(db_session)
    assert [r["category_id"] for r in rows] == [cat.id, None]


def test_revenue_by_category_per_currency_separation(
    db_session, user_factory, product_factory, category_factory, test_shipping_method_id
):
    customer = user_factory()
    cat = category_factory(name="Multi")
    product = product_factory(stock=200, price_usd=10.0, price_pln=40.0, category=cat)
    _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 2)],
        shipping_method_id=test_shipping_method_id,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 3)],
        currency="PLN",
        shipping_method_id=test_shipping_method_id,
    )

    rows = crud_report.revenue_by_category(db_session)
    row = next(r for r in rows if r["category_id"] == cat.id)
    assert row["units"] == 5
    # USD 20 + PLN 120, kept apart.
    assert row["revenue_by_currency"] == {"USD": 20.0, "PLN": 120.0}


# ---------------------------------------------------------------------------
# Refund summary
# ---------------------------------------------------------------------------


def _add_refund(db, *, order_id, amount, currency, created_at: datetime | None = None) -> Refund:
    refund = Refund(
        order_id=order_id,
        amount=amount,
        currency=currency,
        reason="customer_request",
        restock=False,
    )
    db.add(refund)
    db.flush()
    if created_at is not None:
        refund.created_at = created_at
        db.add(refund)
    db.commit()
    return refund


def test_refund_summary_per_currency_totals_and_rate(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory()
    p_usd = product_factory(stock=100, price_usd=100.0)
    p_pln = product_factory(stock=100, price_pln=200.0)
    # Revenue: USD 200 (2 orders of $100), PLN 200 (1 order).
    o1 = _place(
        db_session,
        user_id=customer.id,
        items=[(p_usd.id, 1)],
        shipping_method_id=test_shipping_method_id,
        status=OrderStatus.PAID,
    )
    _place(
        db_session,
        user_id=customer.id,
        items=[(p_usd.id, 1)],
        shipping_method_id=test_shipping_method_id,
        status=OrderStatus.PAID,
    )
    o3 = _place(
        db_session,
        user_id=customer.id,
        items=[(p_pln.id, 1)],
        currency="PLN",
        shipping_method_id=test_shipping_method_id,
        status=OrderStatus.PAID,
    )
    # Refund USD 50 against o1, PLN 200 against o3.
    _add_refund(db_session, order_id=o1.id, amount=50.0, currency="USD")
    _add_refund(db_session, order_id=o3.id, amount=200.0, currency="PLN")

    summary = crud_report.refund_summary(db_session)
    assert summary["refund_count"] == 2
    by_cur = {r["currency"]: r for r in summary["rows"]}

    assert by_cur["USD"]["refunded"] == 50.0
    assert by_cur["USD"]["refund_count"] == 1
    assert by_cur["USD"]["revenue"] == 200.0
    assert by_cur["USD"]["rate"] == round(50.0 / 200.0, 4)  # 0.25

    assert by_cur["PLN"]["refunded"] == 200.0
    assert by_cur["PLN"]["revenue"] == 200.0
    assert by_cur["PLN"]["rate"] == 1.0


def test_refund_summary_date_range_filters_refunds(
    db_session, user_factory, product_factory, test_shipping_method_id
):
    customer = user_factory()
    product = product_factory(stock=100, price_usd=100.0)
    order = _place(
        db_session,
        user_id=customer.id,
        items=[(product.id, 1)],
        shipping_method_id=test_shipping_method_id,
        status=OrderStatus.PAID,
        created_at=datetime(2025, 3, 10, 12, 0, 0),
    )
    # In-range refund + out-of-range refund.
    _add_refund(
        db_session,
        order_id=order.id,
        amount=20.0,
        currency="USD",
        created_at=datetime(2025, 3, 12, 12, 0, 0),
    )
    _add_refund(
        db_session,
        order_id=order.id,
        amount=30.0,
        currency="USD",
        created_at=datetime(2025, 4, 1, 12, 0, 0),
    )

    summary = crud_report.refund_summary(db_session, start=date(2025, 3, 1), end=date(2025, 3, 31))
    assert summary["refund_count"] == 1
    by_cur = {r["currency"]: r for r in summary["rows"]}
    assert by_cur["USD"]["refunded"] == 20.0


def test_refund_summary_rate_zero_when_no_revenue(db_session, user_factory):
    # A refund currency with no comparable revenue → rate 0.0 (no ZeroDivision).
    customer = user_factory()
    # Build a CANCELLED order (not revenue) so EUR has a refund but no revenue.
    order = Order(
        user_id=customer.id,
        order_number="ORD-TEST-EUR-0001",
        status=OrderStatus.CANCELLED,
        subtotal=10.0,
        total=10.0,
        currency="EUR",
        shipping_address="1 St",
        shipping_city="C",
        shipping_postal_code="00-001",
        shipping_country="PL",
    )
    db_session.add(order)
    db_session.flush()
    _add_refund(db_session, order_id=order.id, amount=10.0, currency="EUR")

    summary = crud_report.refund_summary(db_session)
    by_cur = {r["currency"]: r for r in summary["rows"]}
    assert by_cur["EUR"]["refunded"] == 10.0
    assert by_cur["EUR"]["revenue"] == 0.0
    assert by_cur["EUR"]["rate"] == 0.0
