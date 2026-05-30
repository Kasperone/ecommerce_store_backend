"""Unit tests for the pure dimensional-weight helpers.

``line_volume_cm3`` and ``billable_weight_kg`` are pure functions over
floats — no DB, no Pydantic — so they're cheap to exhaust against the
real-world cases the volumetric branch must handle:

- All-dimensions-set → ``l × w × h × qty``
- Any-dimension-NULL → 0 (legacy products contribute nothing — the same
  NULL-as-0 forgiveness as ``weight_kg``)
- Zero dimensions → 0 (a 0 × 0 × 0 box has no volume)
- Divisor NULL / ≤ 0 → return actual weight unchanged (no DIM pricing
  for the zone — backwards-compatible default)
- ``max(actual, volumetric)`` semantics in both directions

These shapes pin the BE side of the contract; the FE TS resolver
(``shared/utils/shippingRate.ts``) port should mirror the same matrix
so the two implementations can never disagree on the same inputs.
"""

from __future__ import annotations

import pytest

from app.services.dimensional_weight import billable_weight_kg, line_volume_cm3


class TestLineVolumeCm3:
    """Per-line ``l × w × h × qty`` with the 'any-dim NULL → 0' safety."""

    def test_all_dimensions_set_multiplies(self):
        # 60 × 60 × 30 = 108_000 cm³ × 1 qty = 108_000
        assert line_volume_cm3(60, 60, 30, 1) == 108_000.0

    def test_quantity_scales_volume(self):
        # Same box × 3 → 3× the volume.
        assert line_volume_cm3(10, 20, 30, 3) == 18_000.0

    def test_length_none_returns_zero(self):
        assert line_volume_cm3(None, 60, 30, 1) == 0.0

    def test_width_none_returns_zero(self):
        assert line_volume_cm3(60, None, 30, 1) == 0.0

    def test_height_none_returns_zero(self):
        assert line_volume_cm3(60, 60, None, 1) == 0.0

    def test_all_none_returns_zero(self):
        """The legacy-product path: no dimensions set anywhere."""
        assert line_volume_cm3(None, None, None, 5) == 0.0

    def test_zero_dimension_returns_zero(self):
        """A 0 × 60 × 30 box has zero volume by definition (not NULL)."""
        assert line_volume_cm3(0, 60, 30, 1) == 0.0
        assert line_volume_cm3(60, 0, 30, 1) == 0.0
        assert line_volume_cm3(60, 60, 0, 1) == 0.0

    def test_quantity_zero_returns_zero(self):
        assert line_volume_cm3(10, 20, 30, 0) == 0.0

    def test_floats_handled(self):
        # 1.5 × 2.5 × 4 × 2 = 30.0
        assert line_volume_cm3(1.5, 2.5, 4.0, 2) == 30.0


class TestBillableWeightKg:
    """``max(actual, volumetric)`` with NULL/zero-divisor short-circuit."""

    def test_divisor_none_returns_actual(self):
        """Zone has no DIM pricing → bills actual weight only."""
        assert billable_weight_kg(2.0, 100_000.0, None) == 2.0

    def test_divisor_zero_returns_actual(self):
        """Divisor 0 would divide-by-zero — treat like NULL (no DIM)."""
        assert billable_weight_kg(2.0, 100_000.0, 0) == 2.0

    def test_divisor_negative_returns_actual(self):
        """Defensive: CHECK constraint blocks negative divisors but the helper
        should still handle a bad value rather than crash."""
        assert billable_weight_kg(2.0, 100_000.0, -1) == 2.0

    def test_volumetric_wins_when_larger(self):
        """The 0.5 kg pillow in a 60×60×30 box — the classic DIM example."""
        # 60 × 60 × 30 = 108_000 cm³ ÷ 5000 = 21.6 kg
        assert billable_weight_kg(0.5, 108_000.0, 5000) == 21.6

    def test_actual_wins_when_larger(self):
        """A dense product where mass exceeds volumetric (an iron weight)."""
        # 1 cm³ ÷ 5000 = 0.0002 kg
        assert billable_weight_kg(10.0, 1.0, 5000) == 10.0

    def test_equal_at_zero(self):
        """Cart of legacy products: no weight, no dimensions, no surprises."""
        assert billable_weight_kg(0.0, 0.0, 5000) == 0.0

    def test_different_divisors_yield_different_billable(self):
        """The 5000 vs 6000 standard the admin tooltip explains."""
        # 108_000 / 5000 = 21.6; 108_000 / 6000 = 18.0
        assert billable_weight_kg(0.5, 108_000.0, 5000) == 21.6
        assert billable_weight_kg(0.5, 108_000.0, 6000) == 18.0

    def test_volume_zero_with_divisor_returns_actual(self):
        """Divisor set but cart has no volume (un-dimensioned products) → actual."""
        assert billable_weight_kg(3.0, 0.0, 5000) == 3.0


@pytest.mark.parametrize(
    ("actual_kg", "volume_cm3", "divisor", "expected"),
    [
        # The walkthrough example from the handoff plan:
        #   0.5 kg actual × (60×60×30) cm³ / 5000 = 21.6 volumetric → 21.6 billable
        (0.5, 108_000.0, 5000, 21.6),
        # Sub-1cm-cube on a 5000 divisor — volumetric collapses, actual wins.
        (10.0, 1.0, 5000, 10.0),
        # International air divisor flattens a smaller box more than ground.
        (0.5, 108_000.0, 6000, 18.0),
        # NULL divisor everywhere → fully ignores the volumetric branch.
        (10.0, 108_000.0, None, 10.0),
        # Zero everything.
        (0.0, 0.0, 5000, 0.0),
        # Volumetric exactly matches actual: equal → either is fine, returns
        # the equal value.
        (5.0, 25_000.0, 5000, 5.0),
    ],
)
def test_billable_weight_kg_matrix(actual_kg, volume_cm3, divisor, expected):
    """Parametrised matrix mirroring the FE TS spec — BE/FE must never drift."""
    assert billable_weight_kg(actual_kg, volume_cm3, divisor) == expected
