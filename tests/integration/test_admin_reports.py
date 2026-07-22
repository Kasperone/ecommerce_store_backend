"""Integration tests for `GET /api/v1/admin/reports/*`.

Covers admin gating (401 unauthenticated / 403 customer) on every endpoint, the
response shapes, the date-range query params, per-currency separation, and the
CSV export. Deep aggregation correctness is unit-tested in
``tests/unit/test_crud_report.py``; here we assert the HTTP contract B-fe builds
against and that the routes are read-only (no audit row written).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.admin_audit_event import AdminAuditEvent
from app.models.order import Order, OrderStatus
from app.models.refund import Refund
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict[str, str]:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _order_body(
    product_id: int, *, shipping_method_id: int, quantity: int = 1, currency: str = "USD"
) -> dict:
    return {
        "items": [{"product_id": product_id, "quantity": quantity}],
        "currency": currency,
        "shipping_address": "1 Test St",
        "shipping_city": "Testville",
        "shipping_postal_code": "00-001",
        "shipping_country": "PL",
        "shipping_method_id": shipping_method_id,
    }


def _naive_utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _place_paid_order(
    client, db_session, customer, product_id, *, shipping_method_id, quantity=1, currency="USD"
) -> int:
    """Create an order via the API and mark it PAID so it counts as revenue."""
    created = await client.post(
        "/api/v1/orders/",
        json=_order_body(
            product_id, shipping_method_id=shipping_method_id, quantity=quantity, currency=currency
        ),
        headers=_headers(customer),
    )
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]
    order = db_session.get(Order, order_id)
    order.status = OrderStatus.PAID
    db_session.add(order)
    db_session.commit()
    return order_id


# ---------------------------------------------------------------------------
# Admin gating — every endpoint
# ---------------------------------------------------------------------------

ENDPOINTS = [
    "/api/v1/admin/reports/sales?start=2025-01-01&end=2025-01-31",
    "/api/v1/admin/reports/top-products",
    "/api/v1/admin/reports/by-category",
    "/api/v1/admin/reports/refund-summary",
    "/api/v1/admin/reports/eu-distance-selling",
    "/api/v1/admin/reports/sales.csv?start=2025-01-01&end=2025-01-31",
]


@pytest.mark.parametrize("url", ENDPOINTS)
async def test_reports_require_auth(client, url):
    resp = await client.get(url)
    assert resp.status_code == 401


@pytest.mark.parametrize("url", ENDPOINTS)
async def test_reports_forbidden_for_customer(client, user_factory, url):
    customer = user_factory()
    resp = await client.get(url, headers=_headers(customer))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------


async def test_sales_report_shape_and_zero_fill(
    client, db_session, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="rep-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="rep-cust@example.com")
    product = product_factory(stock=50, price_usd=20.0)
    await _place_paid_order(
        client,
        db_session,
        customer,
        product.id,
        shipping_method_id=test_shipping_method_id,
        quantity=2,
    )

    today = _naive_utcnow().date()
    start = today - timedelta(days=6)
    resp = await client.get(
        f"/api/v1/admin/reports/sales?start={start}&end={today}&granularity=day",
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["granularity"] == "day"
    assert body["start"] == start.isoformat()
    assert body["end"] == today.isoformat()
    # 7 days inclusive, zero-filled.
    assert len(body["buckets"]) == 7
    point = body["buckets"][-1]
    assert {"bucket", "revenue", "orders"} == set(point.keys())
    # Today's bucket carries the paid order's revenue.
    assert point["orders"] >= 1
    assert point["revenue"].get("USD") == 40.0


async def test_sales_report_per_currency_separation(
    client, db_session, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="rep-cur-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="rep-cur-cust@example.com")
    product = product_factory(stock=50, price_usd=20.0, price_pln=80.0)
    await _place_paid_order(
        client,
        db_session,
        customer,
        product.id,
        shipping_method_id=test_shipping_method_id,
        quantity=1,
    )
    await _place_paid_order(
        client,
        db_session,
        customer,
        product.id,
        shipping_method_id=test_shipping_method_id,
        quantity=1,
        currency="PLN",
    )

    today = _naive_utcnow().date()
    resp = await client.get(
        f"/api/v1/admin/reports/sales?start={today}&end={today}",
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    revenue = resp.json()["buckets"][-1]["revenue"]
    assert revenue == {"USD": 20.0, "PLN": 80.0}


async def test_sales_report_requires_start_end(client, user_factory):
    admin = user_factory(email="rep-missing@example.com", role=UserRole.ADMIN)
    resp = await client.get("/api/v1/admin/reports/sales", headers=_headers(admin))
    assert resp.status_code == 422  # start + end are required


async def test_sales_report_rejects_bad_granularity(client, user_factory):
    admin = user_factory(email="rep-gran@example.com", role=UserRole.ADMIN)
    resp = await client.get(
        "/api/v1/admin/reports/sales?start=2025-01-01&end=2025-01-31&granularity=hour",
        headers=_headers(admin),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Top products
# ---------------------------------------------------------------------------


async def test_top_products_report_shape(
    client, db_session, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="rep-top-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="rep-top-cust@example.com")
    product = product_factory(name="Bestseller", stock=50, price_usd=15.0)
    await _place_paid_order(
        client,
        db_session,
        customer,
        product.id,
        shipping_method_id=test_shipping_method_id,
        quantity=3,
    )

    resp = await client.get("/api/v1/admin/reports/top-products?limit=5", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 5
    assert len(body["rows"]) >= 1
    row = body["rows"][0]
    assert {"product_id", "name", "slug", "units", "order_count", "revenue_by_currency"} == set(
        row.keys()
    )
    assert row["product_id"] == product.id
    assert row["units"] == 3
    assert row["revenue_by_currency"] == {"USD": 45.0}


async def test_top_products_limit_validation(client, user_factory):
    admin = user_factory(email="rep-top-lim@example.com", role=UserRole.ADMIN)
    resp = await client.get("/api/v1/admin/reports/top-products?limit=0", headers=_headers(admin))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# By category
# ---------------------------------------------------------------------------


async def test_by_category_report_shape(
    client, db_session, user_factory, product_factory, category_factory, test_shipping_method_id
):
    admin = user_factory(email="rep-cat-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="rep-cat-cust@example.com")
    category = category_factory(name="Gadgets")
    product = product_factory(name="Gizmo", stock=50, price_usd=10.0, category=category)
    await _place_paid_order(
        client,
        db_session,
        customer,
        product.id,
        shipping_method_id=test_shipping_method_id,
        quantity=4,
    )

    resp = await client.get("/api/v1/admin/reports/by-category", headers=_headers(admin))
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    row = next(r for r in rows if r["category_id"] == category.id)
    assert {"category_id", "name", "units", "revenue_by_currency"} == set(row.keys())
    assert row["name"] == "Gadgets"
    assert row["units"] == 4
    assert row["revenue_by_currency"] == {"USD": 40.0}


# ---------------------------------------------------------------------------
# Refund summary
# ---------------------------------------------------------------------------


async def test_refund_summary_report_shape(
    client, db_session, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="rep-ref-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="rep-ref-cust@example.com")
    product = product_factory(stock=50, price_usd=100.0)
    order_id = await _place_paid_order(
        client,
        db_session,
        customer,
        product.id,
        shipping_method_id=test_shipping_method_id,
        quantity=1,
    )
    # Record a refund directly (the report only reads amount/currency/created_at).
    db_session.add(
        Refund(
            order_id=order_id,
            amount=40.0,
            currency="USD",
            reason="customer_request",
            restock=False,
        )
    )
    db_session.commit()

    resp = await client.get("/api/v1/admin/reports/refund-summary", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["refund_count"] >= 1
    row = next(r for r in body["rows"] if r["currency"] == "USD")
    assert {"currency", "refunded", "refund_count", "revenue", "rate"} == set(row.keys())
    assert row["refunded"] == 40.0
    assert row["revenue"] == 100.0
    assert row["rate"] == round(40.0 / 100.0, 4)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


async def test_sales_csv_export(
    client, db_session, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="rep-csv-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="rep-csv-cust@example.com")
    product = product_factory(stock=50, price_usd=20.0)
    await _place_paid_order(
        client,
        db_session,
        customer,
        product.id,
        shipping_method_id=test_shipping_method_id,
        quantity=2,
    )

    today = _naive_utcnow().date()
    resp = await client.get(
        f"/api/v1/admin/reports/sales.csv?start={today}&end={today}",
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    # UTF-8 BOM (Excel + Polish diacritics) — mirrors the orders export.
    assert resp.content.startswith(b"\xef\xbb\xbf")
    text = resp.content.decode("utf-8-sig")
    lines = text.splitlines()
    assert lines[0] == "bucket,orders,revenue_USD,revenue_PLN,revenue_EUR"
    # One data row for today carrying USD 40.00.
    assert lines[1].startswith(today.isoformat())
    assert "40.00" in lines[1]


# ---------------------------------------------------------------------------
# Read-only invariant — no audit rows written by any report endpoint
# ---------------------------------------------------------------------------


async def test_reports_write_no_audit_rows(
    client, db_session, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="rep-noaudit@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="rep-noaudit-cust@example.com")
    product = product_factory(stock=50, price_usd=20.0)
    await _place_paid_order(
        client,
        db_session,
        customer,
        product.id,
        shipping_method_id=test_shipping_method_id,
        quantity=1,
    )

    before = db_session.query(AdminAuditEvent).count()
    today = _naive_utcnow().date()
    for url in [
        f"/api/v1/admin/reports/sales?start={today}&end={today}",
        "/api/v1/admin/reports/top-products",
        "/api/v1/admin/reports/by-category",
        "/api/v1/admin/reports/refund-summary",
        f"/api/v1/admin/reports/sales.csv?start={today}&end={today}",
    ]:
        resp = await client.get(url, headers=_headers(admin))
        assert resp.status_code == 200
    after = db_session.query(AdminAuditEvent).count()
    assert after == before


# ---------------------------------------------------------------------------
# EU distance-selling threshold
# ---------------------------------------------------------------------------


async def test_eu_distance_selling_shape(client, user_factory):
    """Shape + defaults on an empty store: current + prior year, not approaching."""
    admin = user_factory(email="eu-thr-admin@example.com", role=UserRole.ADMIN)
    resp = await client.get("/api/v1/admin/reports/eu-distance-selling", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["threshold_eur"] == 10000.0
    assert body["liable"] is False
    assert body["effective_status"] == "not_approaching"
    this_year = _naive_utcnow().year
    assert body["current"]["year"] == this_year
    assert body["prior"]["year"] == this_year - 1
    for key in ("total_eur", "pct", "remaining_eur", "status", "unconvertible_count"):
        assert key in body["current"]
    # Empty store: nothing counted, full headroom.
    assert body["current"]["total_eur"] == 0.0
    assert body["current"]["remaining_eur"] == 10000.0


async def test_eu_distance_selling_year_param(client, user_factory):
    admin = user_factory(email="eu-thr-year@example.com", role=UserRole.ADMIN)
    resp = await client.get(
        "/api/v1/admin/reports/eu-distance-selling?year=2024", headers=_headers(admin)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["current"]["year"] == 2024
    assert body["prior"]["year"] == 2023
