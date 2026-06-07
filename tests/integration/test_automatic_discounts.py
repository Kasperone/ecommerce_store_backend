"""Integration tests for automatic discounts + the free_shipping coupon kind.

Covers the order-create path routing through ``pick_best`` (an automatic
applies with no code typed; best-single-discount vs a typed code; an invalid
typed code still 400s; free_shipping zeroes the shipping line with
``discount`` staying 0; the guest no-redemption-row rule extends to
automatics; an exhausted automatic is retried-without rather than failing
checkout) and the reshaped ``POST /coupons/validate`` preview (optional code +
``shipping_amount``, nullable applied-fields on the "nothing applies" 200).
Invoice-PDF + CSV-export smoke for a free_shipping order pins the document-of-
record rendering (shipping line 0, no bogus discount row).

Picker unit tests (tie-breaks, exclusion, eligibility matrix) live in
``tests/unit/test_crud_coupon.py``.
"""

from __future__ import annotations

import csv
import datetime as dt
import io

import pytest
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.crud.coupon import CouponRedemptionError
from app.crud.coupon import coupon as crud_coupon_singleton
from app.crud.order import order as crud_order
from app.crud.shipping import (
    method as crud_shipping_method,
)
from app.crud.shipping import (
    rate as crud_shipping_rate,
)
from app.crud.shipping import (
    zone as crud_shipping_zone,
)
from app.models.coupon import Coupon, CouponRedemption
from app.models.order import Order
from app.schemas.order import OrderCreate
from app.schemas.shipping import (
    ShippingMethodCreate,
    ShippingRateCreate,
    ShippingZoneCreate,
)
from app.services.invoice import StoreIdentity, build_invoice_pdf
from app.services.order_export import CSV_HEADERS, build_orders_csv
from tests.factories.coupon import CouponFactory

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _order_body(product_id: int, *, shipping_method_id: int, country: str = "PL", **extra) -> dict:
    body = {
        "items": [{"product_id": product_id, "quantity": 1}],
        "currency": "USD",
        "shipping_address": "1 Test St",
        "shipping_city": "Testville",
        "shipping_postal_code": "00-001",
        "shipping_country": country,
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


@pytest.fixture
def paid_shipping_method_id(db_session: Session) -> int:
    """A CZ/USD method with a flat $8 rate (the seeded test method is free)."""
    z = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="AutoDisc CZ", country_codes=["CZ"])
    )
    db_session.commit()
    m = crud_shipping_method.create(
        db_session, obj_in=ShippingMethodCreate(name="AutoDisc CZ Std", code="cz-autodisc-std")
    )
    db_session.commit()
    crud_shipping_rate.create(
        db_session,
        obj_in=ShippingRateCreate(zone_id=z.id, method_id=m.id, currency="USD", amount=8.0),
    )
    db_session.commit()
    return m.id


# ---------------------------------------------------------------------------
# POST /orders/  — automatic discounts at checkout
# ---------------------------------------------------------------------------


async def test_create_order_applies_automatic_percent_without_code(
    client, user_factory, product_factory, db_session, coupon_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=100.0)
    coupon = coupon_factory(code="AUTOTEN", is_automatic=True, discount_type="percent", value=10)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=test_shipping_method_id),
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["discount"] == 10.0
    assert body["coupon_code"] == "AUTOTEN"
    assert body["total"] == pytest.approx(100.0 - 10.0 + body["shipping"])

    db_session.expire_all()
    refreshed = db_session.get(Coupon, coupon.id)
    assert refreshed is not None
    assert refreshed.used_count == 1
    # Authed automatic redemption records the per-user row (per_user_limit works).
    rows = db_session.query(CouponRedemption).filter_by(coupon_id=coupon.id).all()
    assert len(rows) == 1
    assert rows[0].user_id == user.id


async def test_create_order_free_shipping_zeroes_shipping_keeps_discount_zero(
    client, user_factory, product_factory, coupon_factory, paid_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=50.0)
    coupon_factory(code="SHIPFREE", is_automatic=True, discount_type="free_shipping", value=0)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=paid_shipping_method_id, country="CZ"),
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["shipping"] == 0.0
    assert body["discount"] == 0.0
    assert body["coupon_code"] == "SHIPFREE"
    # Invariant: total == round(subtotal - discount + shipping, 2)
    assert body["total"] == body["subtotal"] == 50.0


async def test_create_order_bigger_automatic_beats_typed_code(
    client, user_factory, product_factory, coupon_factory, test_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=100.0)
    coupon_factory(code="HAND5", discount_type="percent", value=5)
    coupon_factory(code="AUTO20", is_automatic=True, discount_type="percent", value=20)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, coupon_code="HAND5"
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["coupon_code"] == "AUTO20"
    assert body["discount"] == 20.0


async def test_create_order_invalid_code_400_despite_automatic(
    client, user_factory, product_factory, coupon_factory, test_shipping_method_id
):
    # A bad typed code never silently falls back to an eligible automatic.
    user = user_factory()
    product = product_factory(stock=10, price_usd=100.0)
    coupon_factory(code="AUTOOK", is_automatic=True, discount_type="percent", value=10)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(
            product.id, shipping_method_id=test_shipping_method_id, coupon_code="BOGUS"
        ),
        headers=_headers(user),
    )
    assert resp.status_code == 400


@pytest.mark.rate_limit
async def test_guest_order_applies_automatic_without_redemption_row(
    client, product_factory, coupon_factory, db_session, test_shipping_method_id, reset_limiter
):
    product = product_factory(stock=10, price_usd=100.0)
    coupon = coupon_factory(code="AUTOGUEST", is_automatic=True, discount_type="percent", value=10)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(
            product.id,
            shipping_method_id=test_shipping_method_id,
            guest_email="guest-auto@example.com",
        ),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["discount"] == 10.0
    assert body["coupon_code"] == "AUTOGUEST"

    db_session.refresh(coupon)
    assert coupon.used_count == 1
    assert db_session.query(CouponRedemption).filter_by(coupon_id=coupon.id).all() == []


async def test_create_order_no_code_no_automatic_unchanged(
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


def test_create_with_items_retries_pick_when_automatic_redeem_exhausted(
    db_session, user_factory, product_factory, coupon_factory, test_shipping_method_id, monkeypatch
):
    """An automatic winner losing the last-use race at ``redeem`` is dropped and
    the pick retried without it — the customer never asked for it, so it must
    not fail the checkout. The runner-up automatic applies instead."""
    user = user_factory()
    product = product_factory(stock=10, price_usd=100.0)
    big = coupon_factory(code="AUTORACE20", is_automatic=True, discount_type="percent", value=20)
    small = coupon_factory(code="AUTORACE10", is_automatic=True, discount_type="percent", value=10)

    real_redeem = type(crud_coupon_singleton).redeem

    def racy_redeem(self, db, *, coupon):
        if coupon.id == big.id:
            raise CouponRedemptionError(
                "usage_exhausted", "This coupon has reached its usage limit."
            )
        return real_redeem(self, db, coupon=coupon)

    monkeypatch.setattr(type(crud_coupon_singleton), "redeem", racy_redeem)

    obj_in = OrderCreate(
        items=[{"product_id": product.id, "quantity": 1}],  # pyright: ignore[reportArgumentType] -- pydantic coerces dicts
        currency="USD",
        shipping_address="1 Test St",
        shipping_city="Testville",
        shipping_postal_code="00-001",
        shipping_country="PL",
        shipping_method_id=test_shipping_method_id,
    )
    order = crud_order.create_with_items(db_session, user_id=user.id, obj_in=obj_in)
    db_session.commit()

    assert order.coupon_code == "AUTORACE10"
    assert order.discount == 10.0
    db_session.refresh(small)
    assert small.used_count == 1


# ---------------------------------------------------------------------------
# POST /coupons/validate — preview matrix
# ---------------------------------------------------------------------------


async def test_validate_no_code_returns_automatic(client, coupon_factory):
    coupon_factory(code="AUTOPREVIEW", is_automatic=True, discount_type="percent", value=15)
    resp = await client.post(
        "/api/v1/coupons/validate",
        json={"subtotal": 200.0, "currency": "USD"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "AUTOPREVIEW"
    assert body["discount_type"] == "percent"
    assert body["discount_amount"] == 30.0
    assert body["is_automatic"] is True
    assert body["free_shipping"] is False
    assert body["currency"] == "USD"


async def test_validate_no_code_nothing_applies_200_with_nulls(client):
    resp = await client.post(
        "/api/v1/coupons/validate",
        json={"subtotal": 200.0, "currency": "USD"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] is None
    assert body["discount_type"] is None
    assert body["discount_amount"] is None
    assert body["is_automatic"] is False
    assert body["free_shipping"] is False
    assert body["currency"] == "USD"


async def test_validate_free_shipping_uses_shipping_amount(client, coupon_factory):
    coupon_factory(code="FREESHIP", discount_type="free_shipping", value=0)
    resp = await client.post(
        "/api/v1/coupons/validate",
        json={"code": "freeship", "subtotal": 100.0, "shipping_amount": 12.5, "currency": "USD"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "FREESHIP"
    assert body["discount_type"] == "free_shipping"
    assert body["discount_amount"] == 12.5
    assert body["free_shipping"] is True
    assert body["is_automatic"] is False


async def test_validate_typed_code_still_works_and_carries_new_fields(
    client, user_factory, coupon_factory
):
    user = user_factory()
    coupon_factory(code="SAVE10", discount_type="percent", value=10)
    resp = await client.post(
        "/api/v1/coupons/validate",
        json={"code": "save10", "subtotal": 200.0, "currency": "USD"},
        headers=_headers(user),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "SAVE10"
    assert body["discount_amount"] == 20.0
    assert body["is_automatic"] is False
    assert body["free_shipping"] is False


async def test_validate_invalid_code_400_despite_automatic(client, coupon_factory):
    coupon_factory(code="AUTOBG", is_automatic=True, discount_type="percent", value=10)
    resp = await client.post(
        "/api/v1/coupons/validate",
        json={"code": "NOPE", "subtotal": 100.0, "currency": "USD"},
    )
    assert resp.status_code == 400


async def test_validate_bigger_automatic_wins_over_typed_code(client, coupon_factory):
    coupon_factory(code="HANDP5", discount_type="percent", value=5)
    coupon_factory(code="AUTOP20", is_automatic=True, discount_type="percent", value=20)
    resp = await client.post(
        "/api/v1/coupons/validate",
        json={"code": "HANDP5", "subtotal": 100.0, "currency": "USD"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "AUTOP20"
    assert body["discount_amount"] == 20.0
    assert body["is_automatic"] is True


async def test_validate_blank_code_treated_as_absent(client):
    resp = await client.post(
        "/api/v1/coupons/validate",
        json={"code": "  ", "subtotal": 100.0, "currency": "USD"},
    )
    assert resp.status_code == 200
    assert resp.json()["code"] is None


# ---------------------------------------------------------------------------
# Documents of record: invoice PDF + CSV export smoke for free_shipping orders
# ---------------------------------------------------------------------------


async def test_free_shipping_order_invoice_and_csv_smoke(
    client, user_factory, product_factory, coupon_factory, db_session, paid_shipping_method_id
):
    user = user_factory()
    product = product_factory(stock=10, price_usd=50.0)
    coupon_factory(code="DOCSHIP", is_automatic=True, discount_type="free_shipping", value=0)

    resp = await client.post(
        "/api/v1/orders/",
        json=_order_body(product.id, shipping_method_id=paid_shipping_method_id, country="CZ"),
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    order = db_session.get(Order, resp.json()["id"])
    assert order is not None

    # Invoice PDF renders (shipping line shows 0, no discount row — discount=0).
    store = StoreIdentity(
        name="Test Store",
        address_line="1 Store St",
        postal_city="00-001 Warszawa",
        country="PL",
        tax_id="PL0000000000",
        email="store@example.com",
        vat_rate=23,
    )
    pdf = build_invoice_pdf(
        order, store=store, invoice_number="FV/2026/00001", issued_at=dt.date(2026, 6, 8)
    )
    assert pdf.startswith(b"%PDF")

    # CSV export: shipping 0.00, discount 0.00, coupon code snapshotted.
    csv_bytes = build_orders_csv([order])
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
    record = dict(zip(rows[0], rows[1], strict=True))
    assert set(CSV_HEADERS) >= {"shipping", "discount", "coupon_code"}
    assert record["shipping"] == "0.00"
    assert record["discount"] == "0.00"
    assert record["coupon_code"] == "DOCSHIP"
