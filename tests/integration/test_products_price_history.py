"""Integration tests for the OMNIBUS price-history wiring on product routes.

Covers:
- ``POST /products/`` writes a history row per non-null currency.
- ``PUT /products/{id}`` writes a new row only when the price actually changes
  (chokepoint idempotency holds end-to-end).
- ``GET /products/{id}/lowest-30d?currency=…`` returns the right shape +
  scopes by currency + 404s on a missing product + 422s on bad currency.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.models.price_history import PriceHistory
from app.models.user import UserRole

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


async def test_post_product_records_history_for_each_non_null_currency(
    client, user_factory, category_factory, db_session
):
    admin = user_factory(email="ph-create-admin@example.com", role=UserRole.ADMIN)
    cat = category_factory()
    resp = await client.post(
        "/api/v1/products/",
        json={
            "name": "Test PH",
            "slug": "test-ph-create",
            "sku": "PH-CREATE-001",
            "price_usd": 10.0,
            "price_pln": 40.0,
            # EUR omitted → no row for it.
            "category_id": cat.id,
            "stock": 5,
        },
        headers=_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]

    rows = db_session.query(PriceHistory).filter(PriceHistory.product_id == pid).all()
    assert {(r.currency, r.price) for r in rows} == {("USD", 10.0), ("PLN", 40.0)}


async def test_put_product_writes_row_only_on_price_change(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(email="ph-put-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(price_usd=10.0, price_pln=40.0, price_eur=8.5)

    # First PUT with no price change → no rows written (the factory bypasses the
    # chokepoint, so there's no baseline; the first PUT's no-change path still
    # writes the initial baseline because the chokepoint sees no most-recent row
    # to compare against. That's correct behaviour — we want every "save" to
    # leave a usable history for future comparisons).
    resp = await client.put(
        f"/api/v1/products/{product.id}",
        json={"name": "Renamed"},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    after_first = (
        db_session.query(PriceHistory).filter(PriceHistory.product_id == product.id).count()
    )
    assert after_first == 3  # one per currency on first chokepoint pass

    # Second PUT changing only USD → exactly one new row.
    resp = await client.put(
        f"/api/v1/products/{product.id}",
        json={"price_usd": 8.0},
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    after_second = (
        db_session.query(PriceHistory).filter(PriceHistory.product_id == product.id).count()
    )
    assert after_second == after_first + 1
    usd_rows = (
        db_session.query(PriceHistory)
        .filter(
            PriceHistory.product_id == product.id,
            PriceHistory.currency == "USD",
        )
        .order_by(PriceHistory.effective_from.asc())
        .all()
    )
    assert [r.price for r in usd_rows] == [10.0, 8.0]


async def test_put_product_no_price_change_writes_nothing_after_baseline(
    client, user_factory, product_factory, db_session
):
    """Once a baseline exists, a name-only PUT must not append rows."""
    admin = user_factory(email="ph-noop-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(price_usd=10.0)
    # Seed the baseline via one PUT.
    await client.put(
        f"/api/v1/products/{product.id}",
        json={"name": "Renamed once"},
        headers=_headers(admin),
    )
    baseline = db_session.query(PriceHistory).filter(PriceHistory.product_id == product.id).count()
    # A second name-only PUT must not append.
    await client.put(
        f"/api/v1/products/{product.id}",
        json={"name": "Renamed twice"},
        headers=_headers(admin),
    )
    after = db_session.query(PriceHistory).filter(PriceHistory.product_id == product.id).count()
    assert after == baseline


async def test_get_lowest_30d_returns_currency_scoped_min(
    client, user_factory, product_factory, db_session
):
    admin = user_factory(email="ph-get-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(price_usd=20.0)
    # Seed history by changing the price.
    await client.put(f"/api/v1/products/{product.id}", json={"name": "x"}, headers=_headers(admin))
    await client.put(
        f"/api/v1/products/{product.id}", json={"price_usd": 15.0}, headers=_headers(admin)
    )
    await client.put(
        f"/api/v1/products/{product.id}", json={"price_usd": 18.0}, headers=_headers(admin)
    )

    resp = await client.get(f"/api/v1/products/{product.id}/lowest-30d?currency=USD")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currency"] == "USD"
    assert body["lowest_price"] == 15.0
    assert body["since"] is not None


async def test_get_lowest_30d_404_on_missing_product(client):
    resp = await client.get("/api/v1/products/999999/lowest-30d?currency=USD")
    assert resp.status_code == 404


async def test_get_lowest_30d_422_on_unsupported_currency(client, product_factory):
    product = product_factory(price_usd=10.0)
    resp = await client.get(f"/api/v1/products/{product.id}/lowest-30d?currency=GBP")
    assert resp.status_code == 422


async def test_get_lowest_30d_null_when_no_history(client, product_factory):
    """A product the chokepoint never touched (factory + no PUT) has no rows."""
    product = product_factory(price_usd=10.0)
    resp = await client.get(f"/api/v1/products/{product.id}/lowest-30d?currency=USD")
    assert resp.status_code == 200
    body = resp.json()
    assert body["currency"] == "USD"
    assert body["lowest_price"] is None
    assert body["since"] is None


async def test_get_lowest_30d_is_public(client, product_factory, user_factory):
    """No auth header required — the storefront PDP renders for guests."""
    admin = user_factory(email="ph-public-admin@example.com", role=UserRole.ADMIN)
    product = product_factory(price_usd=10.0)
    await client.put(
        f"/api/v1/products/{product.id}", json={"name": "seed"}, headers=_headers(admin)
    )
    # No Authorization header on this call.
    resp = await client.get(f"/api/v1/products/{product.id}/lowest-30d?currency=USD")
    assert resp.status_code == 200
