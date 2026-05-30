"""Integration tests for the shipping-aware order creation path.

Pins:
- A successful POST /orders snapshots ``shipping_method_id`` + the resolved
  ``shipping`` amount + computed ``total = subtotal + shipping``.
- A method that doesn't have a rate for the asked country+currency 422s with
  ``ShippingRateUnavailableError``.
- A free-above rate zeros the shipping line at the subtotal threshold.
- A tiered rate picks the correct band per cart subtotal.
- Deleting the shipping method NULLs ``shipping_method_id`` on the order but
  keeps the snapshotted ``shipping_method_name``.
"""

from __future__ import annotations

import pytest

from app.core.security import create_access_token
from app.crud.shipping import (
    method as crud_shipping_method,
)
from app.crud.shipping import (
    rate as crud_shipping_rate,
)
from app.crud.shipping import (
    zone as crud_shipping_zone,
)
from app.models.order import Order
from app.schemas.shipping import (
    ShippingMethodCreate,
    ShippingRateCreate,
    ShippingRateTier,
    ShippingZoneCreate,
)

pytestmark = pytest.mark.integration


def _headers(user) -> dict:
    token, _, _ = create_access_token(subject=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _body(
    product_id: int,
    *,
    shipping_method_id: int,
    country: str = "PL",
    currency: str = "USD",
    quantity: int = 1,
) -> dict:
    return {
        "items": [{"product_id": product_id, "quantity": quantity}],
        "currency": currency,
        "shipping_address": "1 Test St",
        "shipping_city": "Test",
        "shipping_postal_code": "00-001",
        "shipping_country": country,
        "shipping_method_id": shipping_method_id,
    }


async def test_order_snapshots_shipping_method(
    client, user_factory, product_factory, test_shipping_method_id
):
    """The seeded Test Zone+Method is free, so shipping=0 + total=subtotal."""
    user = user_factory()
    product = product_factory(stock=10, price_usd=20.0)
    resp = await client.post(
        "/api/v1/orders/",
        json=_body(product.id, shipping_method_id=test_shipping_method_id),
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["shipping_method_id"] == test_shipping_method_id
    assert body["shipping_method_name"] == "Test Shipping"
    assert body["shipping"] == 0.0
    assert body["total"] == body["subtotal"] == 20.0


async def test_order_422_when_method_has_no_rate_for_country(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    """A method that exists but isn't priced for the country+currency 422s.

    The seeded test method is priced for PL/USD only via a zone; pick a country
    NOT in any seeded zone so resolve_one returns None.
    """
    user = user_factory(email="ship-no-rate@example.com")
    product = product_factory(stock=10, price_usd=20.0)
    resp = await client.post(
        "/api/v1/orders/",
        json=_body(product.id, shipping_method_id=test_shipping_method_id, country="ZW"),
        headers=_headers(user),
    )
    assert resp.status_code == 422
    assert "ZW" in resp.json()["detail"]
    assert str(test_shipping_method_id) in resp.json()["detail"]


async def test_order_with_free_above_triggers_zero_shipping(
    client, user_factory, product_factory, db_session
):
    """A subtotal at/above the free threshold persists shipping=0 + total=subtotal."""
    # Seed a fresh zone + method + rate specifically for this test (CZ is not in
    # the session-seeded Test Zone). Keep them isolated so other tests are
    # unaffected (savepoint rollback undoes them at teardown).
    z = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="FreeAbove CZ", country_codes=["CZ"])
    )
    db_session.commit()
    m = crud_shipping_method.create(
        db_session, obj_in=ShippingMethodCreate(name="Free CZ Standard", code="cz-free-std")
    )
    db_session.commit()
    crud_shipping_rate.create(
        db_session,
        obj_in=ShippingRateCreate(
            zone_id=z.id, method_id=m.id, currency="USD", amount=8.0, free_above_subtotal=100
        ),
    )
    db_session.commit()

    user = user_factory(email="ship-freeabove@example.com")
    product = product_factory(stock=10, price_usd=100.0)
    resp = await client.post(
        "/api/v1/orders/",
        json=_body(product.id, shipping_method_id=m.id, country="CZ", currency="USD"),
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # subtotal=100 == free_above=100 → triggers (>=).
    assert body["shipping"] == 0.0
    assert body["total"] == 100.0


async def test_order_with_tiered_rate_picks_correct_band(
    client, user_factory, product_factory, db_session
):
    """The tiered band corresponding to the cart subtotal is applied."""
    z = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="Tiered HU", country_codes=["HU"])
    )
    db_session.commit()
    m = crud_shipping_method.create(
        db_session, obj_in=ShippingMethodCreate(name="Tiered HU Express", code="hu-tier-exp")
    )
    db_session.commit()
    crud_shipping_rate.create(
        db_session,
        obj_in=ShippingRateCreate(
            zone_id=z.id,
            method_id=m.id,
            currency="EUR",
            amount=29.99,
            tiers=[
                ShippingRateTier(min_subtotal=0, amount=29.99),
                ShippingRateTier(min_subtotal=200, amount=14.99),
                ShippingRateTier(min_subtotal=500, amount=4.99),
            ],
        ),
    )
    db_session.commit()

    user = user_factory(email="ship-tiered@example.com")
    # Pick a price that lands in the middle band: 250 → band {200, 14.99} applies.
    product = product_factory(stock=10, price_eur=250.0)
    resp = await client.post(
        "/api/v1/orders/",
        json=_body(product.id, shipping_method_id=m.id, country="HU", currency="EUR"),
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["shipping"] == 14.99
    assert body["total"] == 264.99


async def test_deleting_method_nulls_fk_but_keeps_snapshot(
    client, user_factory, product_factory, db_session, test_shipping_method_id
):
    """SET NULL preserves order history when an admin deprecates a method."""
    user = user_factory(email="ship-snap@example.com")
    product = product_factory(stock=10, price_usd=20.0)
    create = await client.post(
        "/api/v1/orders/",
        json=_body(product.id, shipping_method_id=test_shipping_method_id),
        headers=_headers(user),
    )
    assert create.status_code == 201
    order_id = create.json()["id"]
    snapshot_name = create.json()["shipping_method_name"]
    assert snapshot_name == "Test Shipping"

    # Admin deletes the method by ORM (we don't ship a "delete the seeded
    # session method via the admin route" path because it would break every
    # OTHER test in the session). Direct ORM delete here, then re-read the
    # order to confirm SET NULL + retained snapshot.
    from app.crud import shipping_method as crud_method

    method = crud_method.get(db_session, id=test_shipping_method_id)
    assert method is not None
    db_session.delete(method)
    db_session.commit()

    db_session.expire_all()
    order = db_session.query(Order).filter(Order.id == order_id).first()
    assert order is not None
    assert order.shipping_method_id is None
    assert order.shipping_method_name == snapshot_name
