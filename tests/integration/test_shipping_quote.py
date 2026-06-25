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
    ShippingRateWeightTier,
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


async def test_quote_carries_per_locale_method_copy(client, db_session):
    """name_pl/description_pl (F-PLD-3) flow through to the storefront quote so
    the PL checkout localizes the method without a separate lookup. EN copy is
    retained for the en locale / fallback."""
    z = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="PL Locale Zone", country_codes=["CZ"])
    )
    db_session.commit()
    m = crud_shipping_method.create(
        db_session,
        obj_in=ShippingMethodCreate(
            name="Standard",
            code="pl-std",
            description="Standard parcel delivery",
            name_pl="Kurier standardowy",
            description_pl="Dostawa kurierska (2-5 dni roboczych)",
        ),
    )
    db_session.commit()
    crud_shipping_rate.create(
        db_session,
        obj_in=ShippingRateCreate(zone_id=z.id, method_id=m.id, currency="PLN", amount=12.99),
    )
    db_session.commit()

    resp = await client.get("/api/v1/shipping/quote?country=CZ&currency=PLN&subtotal=50")
    assert resp.status_code == 200
    method = next(x for x in resp.json()["methods"] if x["code"] == "pl-std")
    assert method["name_pl"] == "Kurier standardowy"
    assert method["description_pl"] == "Dostawa kurierska (2-5 dni roboczych)"
    assert method["name"] == "Standard"


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


# ---------------------------------------------------------------------------
# Quote — weight_kg query param + resolved weight-tier band
# ---------------------------------------------------------------------------


async def test_quote_resolves_weight_tier_band(client, db_session):
    """Resolved amount matches the weight band the cart falls into."""
    z = crud_shipping_zone.create_with_countries(
        db_session,
        obj_in=ShippingZoneCreate(name="WeightedZ", country_codes=["CZ"]),
    )
    db_session.commit()
    m = crud_shipping_method.create(
        db_session,
        obj_in=ShippingMethodCreate(name="Weighted Std", code="wt-std"),
    )
    db_session.commit()
    crud_shipping_rate.create(
        db_session,
        obj_in=ShippingRateCreate(
            zone_id=z.id,
            method_id=m.id,
            currency="EUR",
            amount=99.99,
            weight_tiers=[
                ShippingRateWeightTier(max_weight_kg=1, amount=9.99),
                ShippingRateWeightTier(max_weight_kg=5, amount=19.99),
                ShippingRateWeightTier(max_weight_kg=30, amount=49.99),
            ],
        ),
    )
    db_session.commit()

    # 2 kg cart → band 2 → 19.99 EUR.
    resp = await client.get(
        "/api/v1/shipping/quote?country=CZ&currency=EUR&subtotal=50&weight_kg=2"
    )
    assert resp.status_code == 200
    wt = next(x for x in resp.json()["methods"] if x["code"] == "wt-std")
    assert wt["amount"] == 19.99
    assert wt["pricing_kind"] == "weight_tiered"


async def test_quote_weight_kg_defaults_to_zero(client, db_session):
    """Omitting weight_kg → weight branch skipped → flat amount."""
    z = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="DefaultZ", country_codes=["RO"])
    )
    db_session.commit()
    m = crud_shipping_method.create(
        db_session, obj_in=ShippingMethodCreate(name="Default Std", code="d-std")
    )
    db_session.commit()
    crud_shipping_rate.create(
        db_session,
        obj_in=ShippingRateCreate(
            zone_id=z.id,
            method_id=m.id,
            currency="EUR",
            amount=49.99,
            weight_tiers=[ShippingRateWeightTier(max_weight_kg=1, amount=9.99)],
        ),
    )
    db_session.commit()

    # No weight_kg param → defaults to 0 → flat fallback.
    resp = await client.get("/api/v1/shipping/quote?country=RO&currency=EUR&subtotal=50")
    wt = next(x for x in resp.json()["methods"] if x["code"] == "d-std")
    assert wt["amount"] == 49.99
    # Pricing kind still flags the weight-tiered shape regardless of resolution.
    assert wt["pricing_kind"] == "weight_tiered"


async def test_quote_422_on_negative_weight(client):
    resp = await client.get(
        "/api/v1/shipping/quote?country=PL&currency=USD&subtotal=10&weight_kg=-1"
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Quote — cart_volume_cm3 query param + zone dim_divisor (volumetric branch)
# ---------------------------------------------------------------------------


async def test_quote_resolves_volumetric_when_zone_has_divisor(client, db_session):
    """A bulky-light cart with a zone divisor → resolver bills the volumetric band.

    Cart: 0.5 kg actual, 108_000 cm³ (60 × 60 × 30 box).
    Zone: dim_divisor=5000 → volumetric = 21.6 kg → 25 kg band.
    """
    z = crud_shipping_zone.create_with_countries(
        db_session,
        obj_in=ShippingZoneCreate(name="DimDiv Quote Z", country_codes=["NL"], dim_divisor=5000),
    )
    db_session.commit()
    m = crud_shipping_method.create(
        db_session, obj_in=ShippingMethodCreate(name="DimDiv Std", code="dd-std")
    )
    db_session.commit()
    crud_shipping_rate.create(
        db_session,
        obj_in=ShippingRateCreate(
            zone_id=z.id,
            method_id=m.id,
            currency="EUR",
            amount=99.99,
            weight_tiers=[
                ShippingRateWeightTier(max_weight_kg=1, amount=5.0),
                ShippingRateWeightTier(max_weight_kg=25, amount=25.0),
            ],
        ),
    )
    db_session.commit()

    resp = await client.get(
        "/api/v1/shipping/quote"
        "?country=NL&currency=EUR&subtotal=50&weight_kg=0.5&cart_volume_cm3=108000"
    )
    assert resp.status_code == 200
    wt = next(x for x in resp.json()["methods"] if x["code"] == "dd-std")
    # 108_000 / 5000 = 21.6 kg → 25 kg band → 25.0.
    assert wt["amount"] == 25.0


async def test_quote_volume_ignored_when_divisor_null(client, db_session):
    """A zone with NULL divisor keeps actual-weight pricing even if volume is sent.

    Same cart shape as above but no divisor on the zone → 0.5 kg actual wins
    → 1 kg band → $5.
    """
    z = crud_shipping_zone.create_with_countries(
        db_session,
        obj_in=ShippingZoneCreate(name="NoDimDiv Z", country_codes=["BE"]),
    )
    db_session.commit()
    m = crud_shipping_method.create(
        db_session, obj_in=ShippingMethodCreate(name="NoDimDiv Std", code="ndd-std")
    )
    db_session.commit()
    crud_shipping_rate.create(
        db_session,
        obj_in=ShippingRateCreate(
            zone_id=z.id,
            method_id=m.id,
            currency="EUR",
            amount=99.99,
            weight_tiers=[
                ShippingRateWeightTier(max_weight_kg=1, amount=5.0),
                ShippingRateWeightTier(max_weight_kg=25, amount=25.0),
            ],
        ),
    )
    db_session.commit()

    resp = await client.get(
        "/api/v1/shipping/quote"
        "?country=BE&currency=EUR&subtotal=50&weight_kg=0.5&cart_volume_cm3=108000"
    )
    assert resp.status_code == 200
    wt = next(x for x in resp.json()["methods"] if x["code"] == "ndd-std")
    # NULL divisor → volumetric branch skipped → 0.5 kg actual wins → $5.
    assert wt["amount"] == 5.0


async def test_quote_cart_volume_cm3_defaults_to_zero(client, db_session):
    """Omitting cart_volume_cm3 → volumetric branch skipped → actual weight wins."""
    z = crud_shipping_zone.create_with_countries(
        db_session,
        obj_in=ShippingZoneCreate(name="DimDiv Default Z", country_codes=["AT"], dim_divisor=5000),
    )
    db_session.commit()
    m = crud_shipping_method.create(
        db_session, obj_in=ShippingMethodCreate(name="DimDiv Default Std", code="ddd-std")
    )
    db_session.commit()
    crud_shipping_rate.create(
        db_session,
        obj_in=ShippingRateCreate(
            zone_id=z.id,
            method_id=m.id,
            currency="EUR",
            amount=99.99,
            weight_tiers=[
                ShippingRateWeightTier(max_weight_kg=1, amount=5.0),
            ],
        ),
    )
    db_session.commit()

    # No cart_volume_cm3 → 0.5 kg actual → 1 kg band → $5.
    resp = await client.get(
        "/api/v1/shipping/quote?country=AT&currency=EUR&subtotal=50&weight_kg=0.5"
    )
    wt = next(x for x in resp.json()["methods"] if x["code"] == "ddd-std")
    assert wt["amount"] == 5.0


async def test_quote_422_on_negative_volume(client):
    resp = await client.get(
        "/api/v1/shipping/quote" "?country=PL&currency=USD&subtotal=10&cart_volume_cm3=-1"
    )
    assert resp.status_code == 422
