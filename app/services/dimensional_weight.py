"""Pure helpers for dimensional / volumetric shipping-weight pricing.

Real-world courier pricing charges ``max(actual_kg, volumetric_kg)`` — a
0.5 kg pillow in a 60 × 60 × 30 cm box ships at the price of a ~2 kg parcel
because it eats more truck volume than its mass suggests.

These helpers live as pure functions (no SQLAlchemy, no Pydantic) so they
can be unit-tested in isolation and reused from both order create
(``crud_order.create_with_items``) and the public quote endpoint
(``app/api/v1/shipping.py``). They are the single source of truth for the
"billable weight" math; ``apply_shipping_rate`` consumes the resolved
billable as its ``weight_kg`` input and never sees the volumetric branch
directly (per the dimensional-weight handoff D3 — keeps the BE + FE TS
resolvers in lockstep, no signature change).
"""

from __future__ import annotations


def line_volume_cm3(
    length_cm: float | None,
    width_cm: float | None,
    height_cm: float | None,
    quantity: int,
) -> float:
    """Return the per-line volume in cm³, or 0 when any dimension is unknown.

    The "any-dim NULL → 0" branch is the legacy-product safety: a product
    row without all three dimensions contributes nothing to the cart's
    total volume (mirrors how ``weight_kg=None`` contributes 0 to the
    cart's total weight). Convention is "all three set or none" so an
    admin who has filled out length but not width/height still gets a
    sensible 0 contribution.
    """
    if length_cm is None or width_cm is None or height_cm is None:
        return 0.0
    return float(length_cm) * float(width_cm) * float(height_cm) * float(quantity)


def billable_weight_kg(
    total_actual_kg: float,
    total_volume_cm3: float,
    dim_divisor: float | None,
) -> float:
    """Resolve the billable shipping weight: ``max(actual, volumetric)``.

    Volumetric weight = ``total_volume_cm3 / dim_divisor``. The
    ``dim_divisor`` lives on the shipping zone (PL/EU ground ≈ 5000,
    international air ≈ 6000); ``None`` or ``<= 0`` means "no volumetric
    pricing for this zone," in which case the volumetric branch is
    skipped and the actual weight is returned unchanged
    (backwards-compatible default — existing zones without a divisor keep
    billing actual weight only).
    """
    if dim_divisor is None or dim_divisor <= 0:
        return total_actual_kg
    volumetric = total_volume_cm3 / dim_divisor
    return max(total_actual_kg, volumetric)
