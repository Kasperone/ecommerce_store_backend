"""Integration tests for the volumetric-weight pricing path on order create.

Pins the end-to-end shape: a product with dimensions + a zone with
``dim_divisor`` set → ``crud_order.create_with_items`` resolves the
shipping amount against the *billable* weight = ``max(actual, volumetric)``,
not just actual. A NULL ``dim_divisor`` on the zone falls back to actual
weight (backwards-compatible).

The matrix-style proof of correctness lives in
``tests/unit/test_dimensional_weight.py`` (the pure helpers) and
``tests/unit/test_crud_shipping.py::TestApplyShippingRateWeightTiers``
(the resolver). These tests pin that order create wires them together.
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
from app.schemas.shipping import (
    ShippingMethodCreate,
    ShippingRateCreate,
    ShippingRateWeightTier,
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
    country: str,
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


def _seed_weight_banded_zone(db_session, *, country: str, dim_divisor: float | None):
    """Helper: create a zone (with the given divisor) + method + a USD
    weight-banded rate. Returns ``(zone, method)`` so the caller can build
    the order body. Bands cover the 0.5 kg actual / 21.6 kg volumetric
    walkthrough cleanly:

    - up to 1 kg → $5
    - up to 25 kg → $25
    - falls through (≥25) → flat $99
    """
    z = crud_shipping_zone.create_with_countries(
        db_session,
        obj_in=ShippingZoneCreate(
            name=f"DimWeight {country}",
            country_codes=[country],
            dim_divisor=dim_divisor,
        ),
    )
    db_session.commit()
    m = crud_shipping_method.create(
        db_session,
        obj_in=ShippingMethodCreate(
            name=f"DimWeight {country} Std", code=f"dim-{country.lower()}-std"
        ),
    )
    db_session.commit()
    crud_shipping_rate.create(
        db_session,
        obj_in=ShippingRateCreate(
            zone_id=z.id,
            method_id=m.id,
            currency="USD",
            amount=99.0,
            weight_tiers=[
                ShippingRateWeightTier(max_weight_kg=1.0, amount=5.0),
                ShippingRateWeightTier(max_weight_kg=25.0, amount=25.0),
            ],
        ),
    )
    db_session.commit()
    return z, m


async def test_order_with_bulky_light_box_bills_volumetric_band(
    client, user_factory, product_factory, db_session
):
    """The handoff plan's walkthrough: 0.5 kg × 60 × 60 × 30 box, PL divisor 5000.

    Actual = 0.5 kg → falls under the 1 kg band ($5).
    Volumetric = (60 × 60 × 30) / 5000 = 21.6 kg → falls under the 25 kg band ($25).
    Billable = max(0.5, 21.6) = 21.6 → 25 kg band wins → $25 shipping.
    """
    _, method = _seed_weight_banded_zone(db_session, country="AA", dim_divisor=5000)
    user = user_factory(email="dim-bulky-light@example.com")
    product = product_factory(
        stock=10,
        price_usd=20.0,
        weight_kg=0.5,
        length_cm=60.0,
        width_cm=60.0,
        height_cm=30.0,
    )
    resp = await client.post(
        "/api/v1/orders/",
        json=_body(product.id, shipping_method_id=method.id, country="AA"),
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["shipping"] == 25.0, "volumetric 21.6 kg must hit the 25 kg band"
    assert body["total"] == 45.0


async def test_order_with_zone_dim_divisor_null_uses_actual_weight(
    client, user_factory, product_factory, db_session
):
    """Backwards-compat: a zone with NULL divisor ignores volumetric entirely."""
    _, method = _seed_weight_banded_zone(db_session, country="AB", dim_divisor=None)
    user = user_factory(email="dim-null-divisor@example.com")
    # Same box as above but the zone has no divisor → bills against actual 0.5 kg.
    product = product_factory(
        stock=10,
        price_usd=20.0,
        weight_kg=0.5,
        length_cm=60.0,
        width_cm=60.0,
        height_cm=30.0,
    )
    resp = await client.post(
        "/api/v1/orders/",
        json=_body(product.id, shipping_method_id=method.id, country="AB"),
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Actual 0.5 kg falls under the 1 kg band → $5.
    assert body["shipping"] == 5.0
    assert body["total"] == 25.0


async def test_order_with_dense_product_actual_wins(
    client, user_factory, product_factory, db_session
):
    """A heavy small product (10 kg actual, 1 cm³ volumetric) bills actual."""
    _, method = _seed_weight_banded_zone(db_session, country="AC", dim_divisor=5000)
    user = user_factory(email="dim-dense@example.com")
    product = product_factory(
        stock=10,
        price_usd=20.0,
        weight_kg=10.0,
        length_cm=1.0,
        width_cm=1.0,
        height_cm=1.0,
    )
    resp = await client.post(
        "/api/v1/orders/",
        json=_body(product.id, shipping_method_id=method.id, country="AC"),
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # 10 kg actual >> 0.0002 kg volumetric → 25 kg band ($25).
    assert body["shipping"] == 25.0


async def test_order_with_legacy_product_no_dimensions_bills_actual(
    client, user_factory, product_factory, db_session
):
    """A product with NULL length_cm / width_cm / height_cm contributes 0 volume.

    Even on a zone with a divisor set, a legacy product without dimensions
    falls through to actual-weight billing — the "any-dim NULL → 0"
    safety in ``line_volume_cm3``.
    """
    _, method = _seed_weight_banded_zone(db_session, country="AD", dim_divisor=5000)
    user = user_factory(email="dim-legacy-no-dims@example.com")
    product = product_factory(
        stock=10,
        price_usd=20.0,
        weight_kg=0.5,
        # dimensions intentionally omitted — defaults are NULL
    )
    resp = await client.post(
        "/api/v1/orders/",
        json=_body(product.id, shipping_method_id=method.id, country="AD"),
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # 0.5 kg actual + 0 volumetric (NULL dims) → 1 kg band ($5).
    assert body["shipping"] == 5.0


async def test_order_with_quantity_amplifies_volumetric(
    client, user_factory, product_factory, db_session
):
    """Quantity scales line volume linearly: 2 × 60³ box = 432_000 cm³ → 86.4 kg.

    86.4 kg exceeds the 25 kg band → falls through to flat $99.
    """
    _, method = _seed_weight_banded_zone(db_session, country="AE", dim_divisor=5000)
    user = user_factory(email="dim-quantity@example.com")
    product = product_factory(
        stock=10,
        price_usd=20.0,
        weight_kg=0.5,
        length_cm=60.0,
        width_cm=60.0,
        height_cm=30.0,
    )
    resp = await client.post(
        "/api/v1/orders/",
        json=_body(product.id, shipping_method_id=method.id, country="AE", quantity=4),
        headers=_headers(user),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # 4 × (60 × 60 × 30) / 5000 = 86.4 kg billable → over 25 kg band → flat $99.
    assert body["shipping"] == 99.0
