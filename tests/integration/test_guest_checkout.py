"""Integration tests for guest checkout + account-less order tracking.

Covers the optional-auth order create (guest_email required when anonymous,
rejected when authed, IP rate limit), the guest_token issuance, the coupon
no-redemption-row rule, the DB CHECK (owner OR guest email), and the public
``GET /orders/track/{token}`` view (trimmed shape / 404 / rate limit).

Guest *payment* tests live with the provider fixtures in
``test_payments_stripe.py``; guest *email* composition in
``tests/unit/test_order_email.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.models.coupon import Coupon, CouponRedemption
from app.models.order import Order
from tests.factories.coupon import CouponFactory

pytestmark = pytest.mark.integration

GUEST_EMAIL = "guest@example.com"


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _guest_order_body(product_id: int, *, shipping_method_id: int, **extra) -> dict:
    body = {
        "items": [{"product_id": product_id, "quantity": 1}],
        "currency": "USD",
        "shipping_address": "1 Guest St",
        "shipping_city": "Guestville",
        "shipping_postal_code": "00-001",
        "shipping_country": "PL",
        "shipping_method_id": shipping_method_id,
        "guest_email": GUEST_EMAIL,
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
# POST /orders/  (anonymous create)
# ---------------------------------------------------------------------------


async def test_guest_create_201_with_token(client, product_factory, test_shipping_method_id):
    product = product_factory(stock=10, price_usd=25.0)
    resp = await client.post(
        "/api/v1/orders/",
        json=_guest_order_body(product.id, shipping_method_id=test_shipping_method_id),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["user_id"] is None
    assert body["guest_email"] == GUEST_EMAIL
    # 256-bit urlsafe token (43 chars), the guest's tracking/payment credential.
    assert isinstance(body["guest_token"], str)
    assert len(body["guest_token"]) >= 40


async def test_guest_create_stores_locale(
    client, product_factory, db_session, test_shipping_method_id
):
    product = product_factory(stock=10, price_usd=25.0)
    resp = await client.post(
        "/api/v1/orders/",
        json=_guest_order_body(product.id, shipping_method_id=test_shipping_method_id, locale="pl"),
    )
    assert resp.status_code == 201
    order = db_session.get(Order, resp.json()["id"])
    assert order is not None
    assert order.guest_locale == "pl"


async def test_guest_create_invalid_email_422(client, product_factory, test_shipping_method_id):
    product = product_factory(stock=10, price_usd=25.0)
    resp = await client.post(
        "/api/v1/orders/",
        json=_guest_order_body(
            product.id, shipping_method_id=test_shipping_method_id, guest_email="not-an-email"
        ),
    )
    assert resp.status_code == 422


async def test_authed_create_rejects_guest_email_422(
    client, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=25.0)
    resp = await client.post(
        "/api/v1/orders/",
        json=_guest_order_body(product.id, shipping_method_id=test_shipping_method_id),
        headers=_headers(user),
    )
    assert resp.status_code == 422
    assert "guest_email" in resp.json()["detail"]


async def test_authed_order_has_no_guest_fields(
    client, user_factory, product_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=25.0)
    body = _guest_order_body(product.id, shipping_method_id=test_shipping_method_id)
    del body["guest_email"]
    resp = await client.post("/api/v1/orders/", json=body, headers=_headers(user))
    assert resp.status_code == 201
    created = resp.json()
    assert created["user_id"] == user.id
    assert created["guest_email"] is None
    assert created["guest_token"] is None


async def test_guest_create_with_coupon_skips_redemption_row(
    client, product_factory, coupon_factory, db_session, test_shipping_method_id
):
    """Guest coupon use bumps the global used_count but writes NO per-user
    redemption row (CouponRedemption.user_id is NOT NULL — documented
    limitation: per_user_limit is unenforceable for guests)."""
    product = product_factory(stock=10, price_usd=100.0)
    coupon = coupon_factory(code="GUEST10", discount_type="percent", value=10, currency=None)

    resp = await client.post(
        "/api/v1/orders/",
        json=_guest_order_body(
            product.id, shipping_method_id=test_shipping_method_id, coupon_code="GUEST10"
        ),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["discount"] == 10.0
    assert body["coupon_code"] == "GUEST10"

    db_session.refresh(coupon)
    assert coupon.used_count == 1
    redemptions = db_session.query(CouponRedemption).filter_by(coupon_id=coupon.id).all()
    assert redemptions == []


@pytest.mark.rate_limit
async def test_guest_create_rate_limited_10_per_minute(
    client, test_shipping_method_id, reset_limiter
):
    """The anonymous-create IP limit (10/minute) counts every guest attempt —
    the eleventh request 429s before any order work happens. (The bodies
    reference a nonexistent product, so the passing attempts cheaply 422
    *after* the limiter check; authed creates are exempt.)"""
    body = _guest_order_body(999999, shipping_method_id=test_shipping_method_id)
    for _ in range(10):
        resp = await client.post("/api/v1/orders/", json=body)
        assert resp.status_code == 422  # product unavailable — past the limiter
    resp = await client.post("/api/v1/orders/", json=body)
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# CHECK ck_orders_user_or_guest_email
# ---------------------------------------------------------------------------


async def test_ownerless_order_violates_check(db_session):
    order = Order(
        user_id=None,
        guest_email=None,
        order_number="ORD-20260608-CHECK1",
        subtotal=10.0,
        total=10.0,
        shipping_address="1 Nowhere St",
        shipping_city="Nowhere",
        shipping_postal_code="00-000",
        shipping_country="PL",
    )
    db_session.add(order)
    with pytest.raises(IntegrityError, match="ck_orders_user_or_guest_email"):
        db_session.flush()
    db_session.rollback()


# ---------------------------------------------------------------------------
# GET /orders/track/{token}  (public guest tracking)
# ---------------------------------------------------------------------------


async def _create_guest_order(client, product_id: int, shipping_method_id: int) -> dict:
    resp = await client.post(
        "/api/v1/orders/",
        json=_guest_order_body(product_id, shipping_method_id=shipping_method_id),
    )
    assert resp.status_code == 201
    return resp.json()


async def test_track_returns_trimmed_view(client, product_factory, test_shipping_method_id):
    product = product_factory(stock=10, price_usd=25.0)
    created = await _create_guest_order(client, product.id, test_shipping_method_id)

    resp = await client.get(f"/api/v1/orders/track/{created['guest_token']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["order_number"] == created["order_number"]
    assert body["status"] == "pending"
    assert body["total"] == created["total"]
    assert body["currency"] == "USD"
    assert body["shipping_city"] == "Guestville"
    assert body["shipping_country"] == "PL"
    assert len(body["items"]) == 1
    assert body["items"][0]["quantity"] == 1
    # Trimmed: no street address, no ids, no payment/user/guest data.
    for absent in (
        "shipping_address",
        "shipping_postal_code",
        "id",
        "user_id",
        "guest_email",
        "guest_token",
        "payment_intent_id",
        "refunds",
    ):
        assert absent not in body
    # Item lines are snapshots only — nothing enumerable.
    assert "product_id" not in body["items"][0]


async def test_track_unknown_token_404(client):
    resp = await client.get("/api/v1/orders/track/definitely-not-a-token")
    assert resp.status_code == 404


async def test_track_authed_order_not_reachable(
    client, user_factory, product_factory, test_shipping_method_id
):
    # Authed orders have no guest_token; there is no token value that can reach
    # them (sanity check on the lookup column, not just the 404 shape).
    user = user_factory()
    product = product_factory(stock=10, price_usd=25.0)
    body = _guest_order_body(product.id, shipping_method_id=test_shipping_method_id)
    del body["guest_email"]
    created = await client.post("/api/v1/orders/", json=body, headers=_headers(user))
    assert created.status_code == 201
    assert created.json()["guest_token"] is None


@pytest.mark.rate_limit
async def test_track_rate_limited_10_per_minute(
    client, product_factory, test_shipping_method_id, reset_limiter
):
    product = product_factory(stock=10, price_usd=25.0)
    created = await _create_guest_order(client, product.id, test_shipping_method_id)

    for _ in range(10):
        resp = await client.get(f"/api/v1/orders/track/{created['guest_token']}")
        assert resp.status_code == 200
    resp = await client.get(f"/api/v1/orders/track/{created['guest_token']}")
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Guest order visibility (scoped surfaces unchanged)
# ---------------------------------------------------------------------------


async def test_guest_order_invisible_to_other_customers_visible_to_admin(
    client, user_factory, product_factory, test_shipping_method_id
):
    from app.models.user import UserRole

    product = product_factory(stock=10, price_usd=25.0)
    created = await _create_guest_order(client, product.id, test_shipping_method_id)

    customer = user_factory()
    resp = await client.get(f"/api/v1/orders/{created['id']}", headers=_headers(customer))
    assert resp.status_code == 404

    admin = user_factory(email="guest-admin@example.com", role=UserRole.ADMIN)
    resp = await client.get(f"/api/v1/orders/{created['id']}", headers=_headers(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"] is None
    assert body["guest_email"] == GUEST_EMAIL
