"""Integration tests for `GET /api/v1/admin/dashboard/kpis`.

Covers admin gating (401 unauthenticated / 403 customer), the response shape,
and the ``window_days`` query param. Aggregation correctness is unit-tested in
``tests/unit/test_dashboard_crud.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.user import UserRole

pytestmark = pytest.mark.integration

EXPECTED_KEYS = {
    "window_days",
    "generated_at",
    "primary_currency",
    "revenue_total",
    "revenue_window",
    "aov_total",
    "orders_total",
    "orders_window",
    "orders_pending",
    "customers_total",
    "customers_window",
    "sales_trend",
    "top_products",
    "low_stock_count",
    "low_stock",
    "eu_tax_liability",
}


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


async def test_kpis_requires_auth(client):
    resp = await client.get("/api/v1/admin/dashboard/kpis")
    assert resp.status_code == 401


async def test_kpis_forbidden_for_customer(client, user_factory):
    customer = user_factory()
    resp = await client.get("/api/v1/admin/dashboard/kpis", headers=_headers(customer))
    assert resp.status_code == 403


async def test_kpis_admin_returns_full_shape(
    client, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="dash-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="dash-cust@example.com")
    product = product_factory(stock=50, price_usd=20.0)
    created = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=2, shipping_method_id=test_shipping_method_id),
        headers=_headers(customer),
    )
    assert created.status_code == 201

    resp = await client.get("/api/v1/admin/dashboard/kpis", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()

    assert EXPECTED_KEYS.issubset(body.keys())
    assert body["window_days"] == 30
    assert len(body["sales_trend"]) == 30
    assert body["orders_total"] >= 1
    assert body["orders_pending"] >= 1  # the just-placed order is pending
    assert body["customers_total"] >= 1
    # Each trend point carries the expected shape.
    point = body["sales_trend"][-1]
    assert {"date", "order_count", "revenue"} == set(point.keys())
    # EU distance-selling nudge is present and well-formed (empty store ⇒ green).
    liability = body["eu_tax_liability"]
    assert {"status", "pct", "year", "liable"} == set(liability.keys())
    assert liability["status"] == "not_approaching"
    assert liability["liable"] is False


async def test_kpis_window_days_param(client, user_factory):
    admin = user_factory(email="dash-win@example.com", role=UserRole.ADMIN)
    resp = await client.get("/api/v1/admin/dashboard/kpis?window_days=7", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_days"] == 7
    assert len(body["sales_trend"]) == 7

    bad = await client.get("/api/v1/admin/dashboard/kpis?window_days=0", headers=_headers(admin))
    assert bad.status_code == 422


async def test_kpis_custom_date_range(client, user_factory):
    admin = user_factory(email="dash-range@example.com", role=UserRole.ADMIN)
    resp = await client.get(
        "/api/v1/admin/dashboard/kpis?start_date=2026-01-01&end_date=2026-01-10",
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_days"] == 10  # effective inclusive span
    assert len(body["sales_trend"]) == 10
    assert body["sales_trend"][0]["date"] == "2026-01-01"
    assert body["sales_trend"][-1]["date"] == "2026-01-10"


async def test_kpis_start_date_defaults_end_to_today(
    client, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="dash-range-today@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="dash-range-cust@example.com")
    product = product_factory(stock=10, price_usd=15.0)
    created = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=test_shipping_method_id),
        headers=_headers(customer),
    )
    assert created.status_code == 201

    today = datetime.now(UTC).date()
    start = today - timedelta(days=2)
    resp = await client.get(
        f"/api/v1/admin/dashboard/kpis?start_date={start.isoformat()}",
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_days"] == 3  # start, +1 day, today (inclusive)
    assert body["sales_trend"][-1]["date"] == today.isoformat()
    assert body["orders_window"] >= 1  # today's order falls inside the range


async def test_kpis_range_validation(client, user_factory):
    admin = user_factory(email="dash-range-bad@example.com", role=UserRole.ADMIN)
    # end before start
    inverted = await client.get(
        "/api/v1/admin/dashboard/kpis?start_date=2026-02-10&end_date=2026-02-01",
        headers=_headers(admin),
    )
    assert inverted.status_code == 422
    # range longer than the 366-day cap
    too_long = await client.get(
        "/api/v1/admin/dashboard/kpis?start_date=2024-01-01&end_date=2026-01-01",
        headers=_headers(admin),
    )
    assert too_long.status_code == 422
