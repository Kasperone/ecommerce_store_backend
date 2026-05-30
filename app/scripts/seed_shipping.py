"""Seed a baseline shipping configuration for dev / first-deploy.

    python -m app.scripts.seed_shipping

Creates (idempotent — get-or-create on name/code):

- Zones: PL (countries: PL), EU (top 11 active EU markets), RoW (empty —
  admin assigns codes after deciding whether to ship globally).
- Methods: ``standard`` and ``express``.
- Rates per zone × method × currency, demonstrating the three pricing
  shapes:
  - PL × Standard: PLN 19.99 with ``free_above_subtotal=200`` (the PL-market
    "free over 200 zł" anchor), EUR/USD also free-above.
  - PL × Express: PLN **tiered** ``[0=29.99, 300=19.99, 600=9.99]`` to
    demonstrate the band shape; EUR/USD flat.
  - EU × Standard: free-above, EU × Express: flat.
  - RoW: empty (admin adds after assigning countries).

Already-shipped rates are left untouched (rerun the script after edits
without losing admin overrides) — only missing rows are inserted. Methods
not in the seed catalog (``kurier_dpd``, ``inpost_paczkomat``,
``pobranie``) are intentionally left out; admins add them via the
Methods tab. Existing zone country-sets are also left as-is on rerun
so an admin who added/removed a country doesn't get it reverted.

Exits 0 on success; non-zero on any unexpected error so CI / scripted
provisioning fails loudly.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.crud.shipping import rate as crud_shipping_rate
from app.models.shipping import (
    ShippingMethod,
    ShippingRate,
    ShippingZone,
    ShippingZoneCountry,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# Zone catalog
# ---------------------------------------------------------------------------

EU_COUNTRIES = ["DE", "FR", "IT", "ES", "NL", "BE", "AT", "CZ", "SE", "IE", "PT"]

ZONES: list[dict[str, Any]] = [
    {
        "name": "Poland",
        "description": "Domestic shipping within Poland",
        "country_codes": ["PL"],
        "sort_order": 10,
    },
    {
        "name": "European Union",
        "description": "EU member states (selected active markets)",
        "country_codes": EU_COUNTRIES,
        "sort_order": 20,
    },
    {
        "name": "Rest of the World",
        "description": "Countries outside PL and the EU bundle above",
        "country_codes": [],  # admin adds GB/US/CA/etc. via the zone editor
        "sort_order": 30,
    },
]


# ---------------------------------------------------------------------------
# Method catalog
# ---------------------------------------------------------------------------

METHODS: list[dict[str, Any]] = [
    {
        "name": "Standard",
        "code": "standard",
        "description": "Standard parcel delivery (2-5 business days)",
        "sort_order": 10,
    },
    {
        "name": "Express",
        "code": "express",
        "description": "Express delivery (1-2 business days)",
        "sort_order": 20,
    },
]


# ---------------------------------------------------------------------------
# Rate matrix
# ---------------------------------------------------------------------------
#
# Each entry: ``(zone_name, method_code, currency, amount, free_above, tiers)``.
# Tiers shape: ``[(min_subtotal, amount), ...]`` sorted ascending; the CRUD
# canonicalises on insert. Leave ``free_above`` / ``tiers`` as ``None`` for a
# pure flat rate.

RATES: list[tuple[str, str, str, float, float | None, list[tuple[float, float]] | None]] = [
    # PL × Standard — free-above the PL-market anchor of 200 zł.
    ("Poland", "standard", "PLN", 19.99, 200.0, None),
    ("Poland", "standard", "EUR", 4.99, 49.0, None),
    ("Poland", "standard", "USD", 4.99, 49.0, None),
    # PL × Express — tiered PLN to demonstrate band shape; flat EUR/USD.
    ("Poland", "express", "PLN", 29.99, None, [(0.0, 29.99), (300.0, 19.99), (600.0, 9.99)]),
    ("Poland", "express", "EUR", 12.0, None, None),
    ("Poland", "express", "USD", 12.0, None, None),
    # EU × Standard — free-above on each currency.
    ("European Union", "standard", "PLN", 39.99, 250.0, None),
    ("European Union", "standard", "EUR", 9.99, 99.0, None),
    ("European Union", "standard", "USD", 11.0, 99.0, None),
    # EU × Express — flat.
    ("European Union", "express", "PLN", 89.99, None, None),
    ("European Union", "express", "EUR", 19.99, None, None),
    ("European Union", "express", "USD", 22.0, None, None),
    # RoW intentionally empty — admin adds after assigning countries.
]


# ---------------------------------------------------------------------------
# Seeders
# ---------------------------------------------------------------------------


def _get_or_create_zone(db: Session, *, spec: dict[str, Any]) -> ShippingZone:
    """Idempotent zone upsert by name.

    Existing zone country-sets are deliberately left as-is — an admin who
    added/removed a country shouldn't have it reverted by a rerun. Only newly
    created zones get the seed country list.
    """
    zone = db.query(ShippingZone).filter(ShippingZone.name == spec["name"]).first()
    if zone is not None:
        logger.info("zone exists: %s (skip)", spec["name"])
        return zone
    zone = ShippingZone(
        name=spec["name"],
        description=spec.get("description"),
        is_active=True,
        sort_order=spec.get("sort_order", 0),
    )
    db.add(zone)
    db.flush()
    for code in spec.get("country_codes", []):
        db.add(ShippingZoneCountry(zone_id=zone.id, country_code=code))
    db.flush()
    logger.info(
        "zone created: %s with %s countries", spec["name"], len(spec.get("country_codes", []))
    )
    return zone


def _get_or_create_method(db: Session, *, spec: dict[str, Any]) -> ShippingMethod:
    """Idempotent method upsert by code (the carrier identifier)."""
    method = db.query(ShippingMethod).filter(ShippingMethod.code == spec["code"]).first()
    if method is not None:
        logger.info("method exists: %s (skip)", spec["code"])
        return method
    method = ShippingMethod(
        name=spec["name"],
        code=spec["code"],
        description=spec.get("description"),
        sort_order=spec.get("sort_order", 0),
        is_active=True,
    )
    db.add(method)
    db.flush()
    logger.info("method created: %s", spec["code"])
    return method


def _get_or_create_rate(
    db: Session,
    *,
    zone: ShippingZone,
    method: ShippingMethod,
    currency: str,
    amount: float,
    free_above: float | None,
    tiers: list[tuple[float, float]] | None,
) -> ShippingRate:
    """Idempotent rate upsert by ``(zone, method, currency)``.

    Existing rows are left untouched — admin price overrides should survive
    a reseed; only missing rows are inserted.
    """
    existing = crud_shipping_rate.get_for_combo(
        db, zone_id=zone.id, method_id=method.id, currency=currency
    )
    if existing is not None:
        logger.info(
            "rate exists: zone=%s method=%s currency=%s (skip)",
            zone.name,
            method.code,
            currency,
        )
        return existing
    serialised_tiers: list[dict[str, Any]] | None = (
        [{"min_subtotal": float(t), "amount": float(a)} for t, a in sorted(tiers)]
        if tiers
        else None
    )
    rate = ShippingRate(
        zone_id=zone.id,
        method_id=method.id,
        currency=currency,
        amount=amount,
        free_above_subtotal=free_above,
        tiers=serialised_tiers,
    )
    db.add(rate)
    db.flush()
    logger.info(
        "rate created: zone=%s method=%s currency=%s amount=%s (free_above=%s, tiered=%s)",
        zone.name,
        method.code,
        currency,
        amount,
        free_above,
        bool(tiers),
    )
    return rate


def seed_shipping(db: Session) -> None:
    """Idempotent end-to-end seed. Commits once at the end."""
    # Materialise zones by name and methods by code so the rate matrix below
    # can look them up without re-querying per row.
    zones_by_name = {spec["name"]: _get_or_create_zone(db, spec=spec) for spec in ZONES}
    methods_by_code = {spec["code"]: _get_or_create_method(db, spec=spec) for spec in METHODS}

    for zone_name, method_code, currency, amount, free_above, tiers in RATES:
        zone = zones_by_name.get(zone_name)
        method = methods_by_code.get(method_code)
        if zone is None or method is None:
            logger.warning("skip rate: missing zone %s or method %s", zone_name, method_code)
            continue
        _get_or_create_rate(
            db,
            zone=zone,
            method=method,
            currency=currency,
            amount=amount,
            free_above=free_above,
            tiers=tiers,
        )

    db.commit()
    logger.info("shipping seed complete")


def main() -> int:
    db = SessionLocal()
    try:
        seed_shipping(db)
    except Exception:
        logger.exception("shipping seed failed")
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
