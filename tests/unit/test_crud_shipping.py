"""Unit tests for the shipping CRUD + resolver (DB-backed, no HTTP).

Covers zone CRUD with country-set replacement, country-uniqueness conflict
detection, rate UniqueConstraint enforcement at the DB level, the
``resolve_for_country`` + ``resolve_one`` query layer, and the pure
``apply_shipping_rate`` resolver across flat / free-above / tiered shapes.
"""

from __future__ import annotations

from typing import Literal

import pytest
from sqlalchemy.exc import IntegrityError

from app.crud.shipping import (
    ShippingZoneCountryConflictError,
)
from app.crud.shipping import (
    method as crud_shipping_method,
)
from app.crud.shipping import (
    rate as crud_shipping_rate,
)
from app.crud.shipping import (
    zone as crud_shipping_zone,
)
from app.models.shipping import (
    ShippingMethod,
    ShippingRate,
    ShippingZone,
    ShippingZoneCountry,
)
from app.schemas.shipping import (
    ShippingMethodCreate,
    ShippingRateCreate,
    ShippingRateTier,
    ShippingZoneCreate,
    ShippingZoneUpdate,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Resolver — pure function, parametrised across every shape combination
# ---------------------------------------------------------------------------


def _rate(
    *,
    amount: float = 19.99,
    free_above: float | None = None,
    tiers: list[dict[str, float]] | None = None,
) -> ShippingRate:
    """Build a transient (un-persisted) rate for resolver tests."""
    r = ShippingRate(
        zone_id=0,
        method_id=0,
        currency="USD",
        amount=amount,
        free_above_subtotal=free_above,
        tiers=tiers,
    )
    return r


class TestApplyShippingRate:
    """Three precedence layers: free-above → tiers → amount."""

    def test_flat_returns_amount(self):
        r = _rate(amount=19.99)
        assert crud_shipping_rate.apply_shipping_rate(r, 0) == 19.99
        assert crud_shipping_rate.apply_shipping_rate(r, 999) == 19.99

    def test_free_above_triggers_at_threshold(self):
        r = _rate(amount=19.99, free_above=200.0)
        assert crud_shipping_rate.apply_shipping_rate(r, 199.99) == 19.99
        assert crud_shipping_rate.apply_shipping_rate(r, 200.0) == 0.0
        assert crud_shipping_rate.apply_shipping_rate(r, 500) == 0.0

    def test_tiers_pick_largest_min_subtotal_le_subtotal(self):
        r = _rate(
            amount=29.99,
            tiers=[
                {"min_subtotal": 0, "amount": 29.99},
                {"min_subtotal": 300, "amount": 19.99},
                {"min_subtotal": 600, "amount": 9.99},
            ],
        )
        assert crud_shipping_rate.apply_shipping_rate(r, 0) == 29.99
        assert crud_shipping_rate.apply_shipping_rate(r, 299.99) == 29.99
        assert crud_shipping_rate.apply_shipping_rate(r, 300) == 19.99
        assert crud_shipping_rate.apply_shipping_rate(r, 599.99) == 19.99
        assert crud_shipping_rate.apply_shipping_rate(r, 600) == 9.99
        assert crud_shipping_rate.apply_shipping_rate(r, 9999) == 9.99

    def test_free_above_wins_over_tiers(self):
        r = _rate(
            amount=29.99,
            free_above=500.0,
            tiers=[
                {"min_subtotal": 0, "amount": 29.99},
                {"min_subtotal": 300, "amount": 19.99},
            ],
        )
        # Below threshold: tiers still pick.
        assert crud_shipping_rate.apply_shipping_rate(r, 299) == 29.99
        assert crud_shipping_rate.apply_shipping_rate(r, 499) == 19.99
        # At threshold: free-above wins (zero out).
        assert crud_shipping_rate.apply_shipping_rate(r, 500) == 0.0

    def test_tiers_without_matching_band_fall_back_to_amount(self):
        r = _rate(
            amount=19.99,
            tiers=[{"min_subtotal": 100, "amount": 14.99}],
        )
        # No band <= 50 → flat fallback amount.
        assert crud_shipping_rate.apply_shipping_rate(r, 50) == 19.99
        # At the only band's boundary.
        assert crud_shipping_rate.apply_shipping_rate(r, 100) == 14.99

    def test_empty_tiers_treated_like_unset(self):
        r = _rate(amount=19.99, tiers=[])
        assert crud_shipping_rate.apply_shipping_rate(r, 50) == 19.99


# ---------------------------------------------------------------------------
# Zone CRUD
# ---------------------------------------------------------------------------


def test_create_with_countries_persists_codes(db_session):
    z = crud_shipping_zone.create_with_countries(
        db_session,
        obj_in=ShippingZoneCreate(name="Czechia", country_codes=["CZ", "SK"]),
    )
    db_session.commit()

    db_session.refresh(z)
    codes = sorted(c.country_code for c in z.countries)
    assert codes == ["CZ", "SK"]


def test_set_countries_replaces_atomically(db_session):
    z = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="Iberia", country_codes=["ES"])
    )
    db_session.commit()

    crud_shipping_zone.set_countries(db_session, zone=z, codes=["ES", "PT"])
    db_session.commit()
    db_session.refresh(z)
    assert sorted(c.country_code for c in z.countries) == ["ES", "PT"]

    # Remove one, add another in the same call.
    crud_shipping_zone.set_countries(db_session, zone=z, codes=["PT", "AD"])
    db_session.commit()
    db_session.refresh(z)
    assert sorted(c.country_code for c in z.countries) == ["AD", "PT"]


def test_set_countries_raises_on_other_zone_conflict(db_session):
    a = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="Alpha", country_codes=["NO"])
    )
    db_session.commit()
    b = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="Beta", country_codes=[])
    )
    db_session.commit()

    with pytest.raises(ShippingZoneCountryConflictError) as exc:
        crud_shipping_zone.set_countries(db_session, zone=b, codes=["NO"])
    assert exc.value.country_code == "NO"
    assert exc.value.other_zone_id == a.id
    assert exc.value.other_zone_name == "Alpha"


def test_apply_update_partial_keeps_countries_when_omitted(db_session):
    z = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="Update Me", country_codes=["LT", "LV"])
    )
    db_session.commit()

    # PATCH with only name set — country list must survive.
    crud_shipping_zone.apply_update(db_session, zone=z, obj_in=ShippingZoneUpdate(name="Renamed"))
    db_session.commit()
    db_session.refresh(z)
    assert z.name == "Renamed"
    assert sorted(c.country_code for c in z.countries) == ["LT", "LV"]


# ---------------------------------------------------------------------------
# DB-level constraints
# ---------------------------------------------------------------------------


def test_unique_country_code_enforced_at_db_level(db_session):
    z1 = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="Z1", country_codes=["RO"])
    )
    db_session.commit()
    z2 = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="Z2", country_codes=[])
    )
    db_session.commit()
    # Bypass the CRUD's pre-flight check — directly insert a duplicate row to
    # confirm the DB UNIQUE catches it as a defense-in-depth backstop.
    db_session.add(ShippingZoneCountry(zone_id=z2.id, country_code="RO"))
    with pytest.raises(IntegrityError):
        db_session.flush()
    # Ensure z1 still has its row.
    _ = z1  # silence ruff F841 — z1 retained for symmetry


def test_unique_rate_combo_enforced_at_db_level(db_session):
    z = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="Combo Zone", country_codes=["GR"])
    )
    db_session.commit()
    m = crud_shipping_method.create(db_session, obj_in=ShippingMethodCreate(name="Combo Method"))
    db_session.commit()
    crud_shipping_rate.create(
        db_session,
        obj_in=ShippingRateCreate(zone_id=z.id, method_id=m.id, currency="USD", amount=10),
    )
    db_session.commit()
    with pytest.raises(IntegrityError):
        crud_shipping_rate.create(
            db_session,
            obj_in=ShippingRateCreate(zone_id=z.id, method_id=m.id, currency="USD", amount=15),
        )
        db_session.flush()


# ---------------------------------------------------------------------------
# Resolver query layer
# ---------------------------------------------------------------------------


def _setup_zone_method_rate(
    db_session,
    *,
    country: str,
    # Literal-typed (matches ShippingRateCreate.currency) so pyright doesn't
    # flag the schema-construction call below.
    currency: Literal["USD", "PLN", "EUR"],
    amount: float = 5.0,
) -> tuple[ShippingZone, ShippingMethod, ShippingRate]:
    z = crud_shipping_zone.create_with_countries(
        db_session,
        obj_in=ShippingZoneCreate(name=f"Zone-{country}", country_codes=[country]),
    )
    db_session.commit()
    m = crud_shipping_method.create(
        db_session, obj_in=ShippingMethodCreate(name=f"Method-{country}", code=f"m-{country}")
    )
    db_session.commit()
    r = crud_shipping_rate.create(
        db_session,
        obj_in=ShippingRateCreate(zone_id=z.id, method_id=m.id, currency=currency, amount=amount),
    )
    db_session.commit()
    return z, m, r


def test_resolve_for_country_returns_active_only(db_session):
    z, m, _r = _setup_zone_method_rate(db_session, country="HU", currency="EUR")

    pairs = crud_shipping_rate.resolve_for_country(db_session, country_code="HU", currency="EUR")
    assert len(pairs) == 1
    assert pairs[0][0].id == m.id

    # Deactivate the method — no more pairs.
    m.is_active = False
    db_session.commit()
    assert (
        crud_shipping_rate.resolve_for_country(db_session, country_code="HU", currency="EUR") == []
    )

    # Reactivate, deactivate the zone — same result.
    m.is_active = True
    z.is_active = False
    db_session.commit()
    assert (
        crud_shipping_rate.resolve_for_country(db_session, country_code="HU", currency="EUR") == []
    )


def test_resolve_for_country_excludes_mismatched_currency(db_session):
    _setup_zone_method_rate(db_session, country="BG", currency="USD")
    assert (
        crud_shipping_rate.resolve_for_country(db_session, country_code="BG", currency="EUR") == []
    )


def test_resolve_one_returns_none_when_uncovered(db_session):
    _, m, _ = _setup_zone_method_rate(db_session, country="HR", currency="USD")
    # Wrong country.
    assert (
        crud_shipping_rate.resolve_one(
            db_session, country_code="ZZ", currency="USD", method_id=m.id
        )
        is None
    )
    # Wrong currency.
    assert (
        crud_shipping_rate.resolve_one(
            db_session, country_code="HR", currency="EUR", method_id=m.id
        )
        is None
    )
    # Wrong method id.
    assert (
        crud_shipping_rate.resolve_one(
            db_session, country_code="HR", currency="USD", method_id=999_999
        )
        is None
    )


def test_country_in_any_zone(db_session):
    crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="EE Zone", country_codes=["EE"])
    )
    db_session.commit()
    assert crud_shipping_rate.country_in_any_zone(db_session, country_code="EE") is True
    assert crud_shipping_rate.country_in_any_zone(db_session, country_code="XX") is False


# ---------------------------------------------------------------------------
# Schema validation — tiers
# ---------------------------------------------------------------------------


def test_tier_duplicate_min_subtotal_rejected():
    """Two bands at the same threshold would make the resolver non-deterministic."""
    with pytest.raises(ValueError, match="unique"):
        ShippingRateCreate(
            zone_id=1,
            method_id=1,
            currency="USD",
            amount=10,
            tiers=[
                ShippingRateTier(min_subtotal=0, amount=10),
                ShippingRateTier(min_subtotal=0, amount=8),
            ],
        )


def test_empty_tiers_normalised_to_none():
    """The resolver short-circuits on `if tiers:` — empty list must become None."""
    rc = ShippingRateCreate(zone_id=1, method_id=1, currency="USD", amount=10, tiers=[])
    assert rc.tiers is None


def test_tier_negative_min_subtotal_rejected():
    with pytest.raises(ValueError):
        ShippingRateTier(min_subtotal=-1, amount=10)


def test_tier_negative_amount_rejected():
    with pytest.raises(ValueError):
        ShippingRateTier(min_subtotal=0, amount=-1)
