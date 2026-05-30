"""Integration tests for the admin orders CSV export + list date filters.

Covers the admin-gated ``GET /orders/export`` (auth, content type, filename,
status + date narrowing, parseable body) and the new ``date_from``/``date_to``
query params on ``GET /orders/``. Order CSV export plan:
``ecommerce_store/.claude/notes/plans/orders/in-progress/2026-05-28-order-csv-export.md``.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest

from app.core.security import create_access_token
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
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


def _data_rows(content: bytes) -> list[list[str]]:
    """Parse the CSV body (BOM-stripped) and drop the header row."""
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    assert rows[0][0] == "order_number"
    return rows[1:]


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _days_from_now(n: int) -> str:
    return (datetime.now(UTC).date() + timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# GET /orders/export
# ---------------------------------------------------------------------------


async def test_export_forbidden_for_customer(client, user_factory):
    customer = user_factory(email="exp-cust@example.com")
    resp = await client.get("/api/v1/orders/export", headers=_headers(customer))
    assert resp.status_code == 403


async def test_export_returns_parseable_csv_for_admin(
    client, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="exp-admin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="exp-buyer@example.com")
    product = product_factory(stock=10, price_usd=25.0)
    await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=2, shipping_method_id=test_shipping_method_id),
        headers=_headers(customer),
    )

    resp = await client.get("/api/v1/orders/export", headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    disp = resp.headers["content-disposition"]
    assert "attachment" in disp
    assert disp.endswith('.csv"')

    rows = _data_rows(resp.content)
    assert len(rows) == 1  # one row per order


async def test_export_does_not_collide_with_detail_route(client, user_factory):
    # The literal "/export" must resolve to the export route, never be parsed as
    # an int order_id (which would 422). A customer hitting it gets 403, not 422.
    customer = user_factory(email="exp-route@example.com")
    resp = await client.get("/api/v1/orders/export", headers=_headers(customer))
    assert resp.status_code == 403


async def test_export_status_filter_narrows(
    client, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="exp-statusadmin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="exp-statuscust@example.com")
    product = product_factory(stock=20, price_usd=10.0)
    created = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(customer),
    )
    await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(customer),
    )
    # Cancel one so the two orders sit in different statuses.
    await client.post(f"/api/v1/orders/{created.json()['id']}/cancel", headers=_headers(customer))

    cancelled = await client.get("/api/v1/orders/export?status=cancelled", headers=_headers(admin))
    pending = await client.get("/api/v1/orders/export?status=pending", headers=_headers(admin))
    assert len(_data_rows(cancelled.content)) == 1
    assert len(_data_rows(pending.content)) == 1
    # Filename reflects the status filter.
    assert "orders-cancelled" in cancelled.headers["content-disposition"]


async def test_export_date_range_narrows(
    client, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="exp-dateadmin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="exp-datecust@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(customer),
    )

    # A future-only window excludes today's order (header-only body).
    future = await client.get(
        f"/api/v1/orders/export?date_from={_days_from_now(5)}", headers=_headers(admin)
    )
    assert len(_data_rows(future.content)) == 0

    # A window spanning today includes it.
    spanning = await client.get(
        f"/api/v1/orders/export?date_from={_days_from_now(-5)}&date_to={_days_from_now(5)}",
        headers=_headers(admin),
    )
    assert len(_data_rows(spanning.content)) == 1
    assert _days_from_now(-5) in spanning.headers["content-disposition"]


# ---------------------------------------------------------------------------
# GET /orders/  date filters (export = what you see)
# ---------------------------------------------------------------------------


async def test_list_date_from_filters(
    client, user_factory, product_factory, test_shipping_method_id
):
    admin = user_factory(email="list-dateadmin@example.com", role=UserRole.ADMIN)
    customer = user_factory(email="list-datecust@example.com")
    product = product_factory(stock=10, price_usd=10.0)
    await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, quantity=1, shipping_method_id=test_shipping_method_id),
        headers=_headers(customer),
    )

    future = await client.get(
        f"/api/v1/orders/?date_from={_days_from_now(5)}", headers=_headers(admin)
    )
    assert future.json()["total"] == 0

    today = await client.get(
        f"/api/v1/orders/?date_from={_today()}&date_to={_today()}", headers=_headers(admin)
    )
    assert today.json()["total"] == 1


async def test_list_invalid_date_422(client, user_factory):
    admin = user_factory(email="list-baddate@example.com", role=UserRole.ADMIN)
    resp = await client.get("/api/v1/orders/?date_from=not-a-date", headers=_headers(admin))
    assert resp.status_code == 422
