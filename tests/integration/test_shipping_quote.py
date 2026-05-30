"""Integration tests for the public shipping quote endpoint.

GET /api/v1/shipping/quote — no auth, returns the resolved methods + prices
for a (country, currency, subtotal) triple. Empty methods=[] is the
canonical "we don't ship there" signal (not 404).
"""

from __future__ import annotations

import pytest

from app.crud.shipping import (
    method as crud_shipping_method,
)
from app.crud.shipping import (
    rate as crud_shipping_rate,
)
from app.crud.shipping import (
    zone as crud_shipping_zone,
)
from app.schemas.shipping import (
    ShippingMethodCreate,
    ShippingRateCreate,
    ShippingRateTier,
    ShippingZoneCreate,
)

pytestmark = pytest.mark.integration


async def test_quote_returns_seeded_test_method(client):
    """The session-seeded Test Zone + Test Method covers PL/USD by default."""
    resp = await client.get("/api/v1/shipping/quote?country=PL&currency=USD&subtotal=99.99")
    assert resp.status_code == 200
    body = resp.json()
    assert body["country_code"] == "PL"
    assert body["currency"] == "USD"
    assert body["subtotal"] == 99.99
    methods = body["methods"]
    # At least the seeded test method.
    assert any(m["code"] == "test" for m in methods)
    test_method = next(m for m in methods if m["code"] == "test")
    assert test_method["amount"] == 0.0
    assert test_method["is_free"] is False  # no free_above set
    assert test_method["pricing_kind"] == "flat"


async def test_quote_empty_methods_when_country_uncovered(client):
    resp = await client.get("/api/v1/shipping/quote?country=ZW&currency=USD&subtotal=10")
    assert resp.status_code == 200
    assert resp.json()["methods"] == []


async def test_quote_returns_resolved_free_above(client, db_session):
    # NB: the session-seeded Test Zone covers PL/DE/FR/US/GB, so pick a country
    # outside it for isolation (here HU — also used by the tiered test below).
    z = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="FreeAbove Zone", country_codes=["GR"])
    )
    db_session.commit()
    m = crud_shipping_method.create(
        db_session,
        obj_in=ShippingMethodCreate(name="FreeAbove Standard", code="fa-std", sort_order=1),
    )
    db_session.commit()
    crud_shipping_rate.create(
        db_session,
        obj_in=ShippingRateCreate(
            zone_id=z.id, method_id=m.id, currency="EUR", amount=9.99, free_above_subtotal=99
        ),
    )
    db_session.commit()

    # Below threshold: pays the flat amount.
    below = await client.get("/api/v1/shipping/quote?country=GR&currency=EUR&subtotal=50")
    fa = next(x for x in below.json()["methods"] if x["code"] == "fa-std")
    assert fa["amount"] == 9.99
    assert fa["is_free"] is False
    assert fa["pricing_kind"] == "free_above"

    # At threshold: free.
    at = await client.get("/api/v1/shipping/quote?country=GR&currency=EUR&subtotal=99")
    fa = next(x for x in at.json()["methods"] if x["code"] == "fa-std")
    assert fa["amount"] == 0.0
    assert fa["is_free"] is True


async def test_quote_returns_resolved_tiered(client, db_session):
    z = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="Tiered Zone", country_codes=["HU"])
    )
    db_session.commit()
    m = crud_shipping_method.create(
        db_session, obj_in=ShippingMethodCreate(name="Tiered Express", code="tier-exp")
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
            ],
        ),
    )
    db_session.commit()

    low = await client.get("/api/v1/shipping/quote?country=HU&currency=EUR&subtotal=50")
    te = next(x for x in low.json()["methods"] if x["code"] == "tier-exp")
    assert te["amount"] == 29.99
    assert te["pricing_kind"] == "tiered"

    high = await client.get("/api/v1/shipping/quote?country=HU&currency=EUR&subtotal=300")
    te = next(x for x in high.json()["methods"] if x["code"] == "tier-exp")
    assert te["amount"] == 14.99


async def test_quote_excludes_inactive_methods(client, db_session):
    z = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="Inactive Zone", country_codes=["IT"])
    )
    db_session.commit()
    m = crud_shipping_method.create(
        db_session,
        obj_in=ShippingMethodCreate(name="Inactive Method", code="inact", is_active=False),
    )
    db_session.commit()
    crud_shipping_rate.create(
        db_session,
        obj_in=ShippingRateCreate(zone_id=z.id, method_id=m.id, currency="EUR", amount=5),
    )
    db_session.commit()

    resp = await client.get("/api/v1/shipping/quote?country=IT&currency=EUR&subtotal=10")
    assert all(meth["code"] != "inact" for meth in resp.json()["methods"])


async def test_quote_422_on_bad_country(client):
    resp = await client.get("/api/v1/shipping/quote?country=poland&currency=USD&subtotal=10")
    assert resp.status_code == 422


async def test_quote_422_on_unsupported_currency(client):
    resp = await client.get("/api/v1/shipping/quote?country=PL&currency=GBP&subtotal=10")
    assert resp.status_code == 422


async def test_quote_422_on_negative_subtotal(client):
    resp = await client.get("/api/v1/shipping/quote?country=PL&currency=USD&subtotal=-1")
    assert resp.status_code == 422


async def test_quote_no_auth_required(client):
    """Anonymous customers fetch quotes — no token sent."""
    resp = await client.get("/api/v1/shipping/quote?country=PL&currency=USD&subtotal=0")
    assert resp.status_code == 200
