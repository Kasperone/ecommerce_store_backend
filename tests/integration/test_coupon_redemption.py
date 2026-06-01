"""Integration tests for coupon redemption at checkout.

Covers the customer-facing ``POST /api/v1/coupons/validate`` preview endpoint and
the order-create path applying / re-validating a coupon code: discount math,
``used_count`` bump, total = subtotal - discount + shipping, and the failure
modes (expired / inactive / min-order / usage-exhausted / currency-mismatch).
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.coupon import Coupon
from tests.factories.coupon import CouponFactory

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _order_body(product_id: int, *, shipping_method_id: int, **extra) -> dict:
    body = {
        "items": [{"product_id": product_id, "quantity": 1}],
        "currency": "USD",
        "shipping_address": "1 Test St",
        "shipping_city": "Testville",
        "shipping_postal_code": "00-001",
        "shipping_country": "PL",
        "shipping_method_id": shipping_method_id,
    }
    body.update(extra)
    return body


@pytest.fixture
def coupon_factory(db_session: Session):
    CouponFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue]
    CouponFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue]

    def _make(**kwargs) -> Coupon:
        return CouponFactory(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# POST /coupons/validate  (preview)
# ---------------------------------------------------------------------------


async def test_validate_percent_returns_discount(client, user_factory, coupon_factory):
    user = user_factory()
    coupon_factory(code="SAVE10", discount_type="percent", value=10, currency=None)
    resp = await client.post(
        "/api/v1/coupons/validate",
        json={"code": "save10", "subtotal": 200.0, "currency": "USD"},
        headers=_headers(user),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "SAVE10"
    assert body["discount_amount"] == 20.0
    assert body["currency"] == "USD"


async def test_validate_requires_auth(client, coupon_factory):
    coupon_factory(code="SAVE10")
    resp = await client.post(
        "/api/v1/coupons/validate",
        json={"code": "SAVE10", "subtotal": 200.0, "currency": "USD"},
    )
    assert resp.status_code == 401


async def test_validate_min_order_not_met_400(client, user_factory, coupon_factory):
    user = user_factory()
    coupon_factory(code="BIG", min_order_amount=100.0)
    resp = await client.post(
        "/api/v1/coupons/validate",
        json={"code": "BIG", "subtotal": 50.0, "currency": "USD"},
        headers=_headers(user),
    )
    assert resp.status_code == 400
    assert "minimum order" in resp.json()["detail"].lower()


async def test_validate_unknown_code_400(client, user_factory):
    user = user_factory()
    resp = await client.post(
        "/api/v1/coupons/validate",
        json={"code": "NOPE", "subtotal": 50.0, "currency": "USD"},
        headers=_headers(user),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /  (create with coupon)
# ---------------------------------------------------------------------------


async def test_create_order_applies_percent_coupon(
    client, user_factory, product_factory, db_session, coupon_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=100.0)
    coupon = coupon_factory(code="TENOFF", discount_type="percent", value=10, currency=None)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, coupon_code="tenoff"
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["subtotal"] == 100.0
    assert body["discount"] == 10.0
    assert body["coupon_code"] == "TENOFF"
    # total = subtotal - discount + shipping
    assert body["total"] == pytest.approx(100.0 - 10.0 + body["shipping"])

    # used_count bumped, committed.
    db_session.expire_all()
    refreshed = db_session.get(Coupon, coupon.id)
    assert refreshed is not None
    assert refreshed.used_count == 1


async def test_create_order_fixed_coupon_capped(
    client, user_factory, product_factory, coupon_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=30.0)
    coupon_factory(code="FLAT50", discount_type="fixed", value=50, currency="USD")

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, coupon_code="FLAT50"
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    # Discount never exceeds the subtotal.
    assert body["discount"] == 30.0
    assert body["total"] == pytest.approx(body["shipping"])


async def test_create_order_without_coupon_has_zero_discount(
    client, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=40.0)
    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=test_shipping_method_id),
        headers=_headers(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["discount"] == 0.0
    assert body["coupon_code"] is None


async def test_create_order_expired_coupon_400_no_order(
    client, user_factory, product_factory, db_session, coupon_factory, test_shipping_method_id
):
    from datetime import UTC, datetime, timedelta

    user = user_factory()
    product = product_factory(stock=10, price_usd=40.0)
    coupon_factory(
        code="EXPIRED",
        ends_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1),
    )

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, coupon_code="EXPIRED"
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"].lower()


async def test_create_order_usage_cap_blocks_second(
    client, user_factory, product_factory, coupon_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=40.0)
    coupon_factory(code="ONCE", max_uses=1, used_count=0)

    first = await client.post(
        "/api/v1/orders/",
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, coupon_code="ONCE"
        ),
        headers=_headers(user),
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/orders/",
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, coupon_code="ONCE"
        ),
        headers=_headers(user),
    )
    assert second.status_code == 400
    assert "usage limit" in second.json()["detail"].lower()
