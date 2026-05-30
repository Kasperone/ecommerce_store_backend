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
    ShippingRateWeightTier,
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
    weight_tiers: list[dict[str, float]] | None = None,
) -> ShippingRate:
    """Build a transient (un-persisted) rate for resolver tests."""
    r = ShippingRate(
        zone_id=0,
        method_id=0,
        currency="USD",
        amount=amount,
        free_above_subtotal=free_above,
        tiers=tiers,
        weight_tiers=weight_tiers,
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
# Weight tiers — D1 precedence (free-above → weight → subtotal → flat) and
# legacy-product fall-through (weight=0 skips the weight branch cleanly).
# ---------------------------------------------------------------------------


_WEIGHT_BANDS = [
    {"max_weight_kg": 1.0, "amount": 9.99},
    {"max_weight_kg": 5.0, "amount": 19.99},
    {"max_weight_kg": 30.0, "amount": 49.99},
]


class TestApplyShippingRateWeightTiers:
    """Weight-tier precedence + boundary semantics ('smallest covering band')."""

    @pytest.mark.parametrize(
        ("weight", "expected"),
        [
            (0.5, 9.99),  # under band 1's max → band 1 covers
            (1.0, 9.99),  # at band 1's max boundary → band 1 still covers
            (1.01, 19.99),  # just over band 1 → band 2
            (5.0, 19.99),  # at band 2's max → band 2 still covers
            (5.01, 49.99),  # just over band 2 → band 3
            (30.0, 49.99),  # at band 3's max → band 3 still covers
        ],
    )
    def test_weight_tiers_pick_smallest_covering_band(self, weight, expected):
        r = _rate(amount=99.99, weight_tiers=_WEIGHT_BANDS)
        assert crud_shipping_rate.apply_shipping_rate(r, 0, weight_kg=weight) == expected

    def test_weight_above_largest_band_falls_through_to_subtotal_or_flat(self):
        """30 kg is the largest band — 31 kg has no covering band, falls through."""
        r = _rate(
            amount=99.99,
            weight_tiers=_WEIGHT_BANDS,
            tiers=[{"min_subtotal": 0, "amount": 79.99}],
        )
        # Weight > biggest band → skip weight branch → subtotal-tier picks band 0.
        assert crud_shipping_rate.apply_shipping_rate(r, 50, weight_kg=31) == 79.99
        # Same, no subtotal tier → flat amount.
        r2 = _rate(amount=99.99, weight_tiers=_WEIGHT_BANDS)
        assert crud_shipping_rate.apply_shipping_rate(r2, 50, weight_kg=31) == 99.99

    def test_free_above_wins_over_weight_tiers(self):
        """Customer-facing 'free over X' beats any weight ladder."""
        r = _rate(amount=99.99, free_above=500.0, weight_tiers=_WEIGHT_BANDS)
        # Below threshold: weight bracket applies.
        assert crud_shipping_rate.apply_shipping_rate(r, 499, weight_kg=2) == 19.99
        # At threshold: free-above zeroes out regardless of weight.
        assert crud_shipping_rate.apply_shipping_rate(r, 500, weight_kg=2) == 0.0
        assert crud_shipping_rate.apply_shipping_rate(r, 9999, weight_kg=20) == 0.0

    def test_weight_tiers_win_over_subtotal_tiers(self):
        """D1: weight is the primary physical signal, beats subtotal tiers."""
        r = _rate(
            amount=99.99,
            weight_tiers=_WEIGHT_BANDS,
            tiers=[
                {"min_subtotal": 0, "amount": 14.99},
                {"min_subtotal": 300, "amount": 4.99},
            ],
        )
        # 2 kg cart with a $400 subtotal: weight-tier band 2 wins ($19.99), NOT
        # subtotal-tier band 1 ($4.99). Confirms weight precedence.
        assert crud_shipping_rate.apply_shipping_rate(r, 400, weight_kg=2) == 19.99

    def test_weight_zero_skips_weight_tiers(self):
        """D2: legacy products with NULL weight (cart_weight=0) fall through cleanly."""
        r = _rate(
            amount=99.99,
            weight_tiers=_WEIGHT_BANDS,
            tiers=[{"min_subtotal": 0, "amount": 14.99}],
        )
        # Cart of legacy products (all NULL weights → total 0) → weight branch
        # skipped → subtotal tier picks.
        assert crud_shipping_rate.apply_shipping_rate(r, 100, weight_kg=0) == 14.99
        # No subtotal tier either → flat amount.
        r2 = _rate(amount=99.99, weight_tiers=_WEIGHT_BANDS)
        assert crud_shipping_rate.apply_shipping_rate(r2, 100, weight_kg=0) == 99.99

    def test_empty_weight_tiers_treated_like_unset(self):
        r = _rate(amount=19.99, weight_tiers=[])
        assert crud_shipping_rate.apply_shipping_rate(r, 50, weight_kg=2) == 19.99

    def test_weight_kg_default_keeps_legacy_call_sites_working(self):
        """No-kwarg calls (admin previews pre-weight) still resolve subtotal-only."""
        r = _rate(amount=19.99, weight_tiers=_WEIGHT_BANDS)
        # Default weight_kg=0 → weight branch skipped → flat amount.
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


# ---------------------------------------------------------------------------
# Schema validation — weight_tiers
# ---------------------------------------------------------------------------


def test_weight_tier_duplicate_max_weight_kg_rejected():
    """Two bands at the same threshold would make 'smallest covering' non-deterministic."""
    with pytest.raises(ValueError, match="unique"):
        ShippingRateCreate(
            zone_id=1,
            method_id=1,
            currency="USD",
            amount=10,
            weight_tiers=[
                ShippingRateWeightTier(max_weight_kg=1, amount=9.99),
                ShippingRateWeightTier(max_weight_kg=1, amount=8.99),
            ],
        )


def test_empty_weight_tiers_normalised_to_none():
    """The resolver short-circuits on `if weight_tiers:` — empty list must become None."""
    rc = ShippingRateCreate(zone_id=1, method_id=1, currency="USD", amount=10, weight_tiers=[])
    assert rc.weight_tiers is None


def test_weight_tier_nonpositive_max_weight_kg_rejected():
    """A zero/negative max_weight_kg makes no physical sense (band 'covers' nothing)."""
    with pytest.raises(ValueError):
        ShippingRateWeightTier(max_weight_kg=0, amount=10)
    with pytest.raises(ValueError):
        ShippingRateWeightTier(max_weight_kg=-1, amount=10)


def test_weight_tier_negative_amount_rejected():
    with pytest.raises(ValueError):
        ShippingRateWeightTier(max_weight_kg=1, amount=-1)


def test_weight_tiers_canonicalised_sorted_on_persist(db_session):
    """The CRUD's _serialise_weight_tiers sorts ascending by max_weight_kg."""
    z = crud_shipping_zone.create_with_countries(
        db_session, obj_in=ShippingZoneCreate(name="Weights Zone", country_codes=["XK"])
    )
    db_session.commit()
    m = crud_shipping_method.create(db_session, obj_in=ShippingMethodCreate(name="Weights Method"))
    db_session.commit()
    r = crud_shipping_rate.create(
        db_session,
        obj_in=ShippingRateCreate(
            zone_id=z.id,
            method_id=m.id,
            currency="USD",
            amount=99.99,
            # Posted out of order on purpose — persist must sort.
            weight_tiers=[
                ShippingRateWeightTier(max_weight_kg=30, amount=49.99),
                ShippingRateWeightTier(max_weight_kg=1, amount=9.99),
                ShippingRateWeightTier(max_weight_kg=5, amount=19.99),
            ],
        ),
    )
    db_session.commit()
    db_session.refresh(r)
    assert r.weight_tiers is not None
    assert [b["max_weight_kg"] for b in r.weight_tiers] == [1.0, 5.0, 30.0]
