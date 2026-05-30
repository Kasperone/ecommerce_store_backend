"""CRUD operations for shipping zones, methods, and rates.

The single arithmetic entry point is ``CRUDShippingRate.apply_shipping_rate``:
the order-create path, the public quote endpoint, and admin previews all call
it so the resolver never drifts. Resolver precedence is free-above-trigger →
subtotal-tier → flat amount (see ``app/models/shipping.py`` module docstring
for the rationale).

CRUD methods never commit — the caller (route handler) owns the transaction
boundary, same convention as the rest of the CRUD layer (``CRUDBase``,
``CRUDOrder``, ``crud_inventory.apply_delta``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import exists, func
from sqlalchemy.orm import Session, joinedload, selectinload

from app.crud.base import CRUDBase
from app.models.shipping import (
    ShippingMethod,
    ShippingRate,
    ShippingZone,
    ShippingZoneCountry,
)
from app.schemas.shipping import (
    ShippingMethodCreate,
    ShippingMethodUpdate,
    ShippingRateCreate,
    ShippingRateTier,
    ShippingRateUpdate,
    ShippingZoneCreate,
    ShippingZoneUpdate,
)


class ShippingZoneCountryConflictError(Exception):
    """A country code is already assigned to a different zone.

    Raised by ``CRUDShippingZone.set_countries`` so the admin route can map
    it to a 400 with the conflicting zone's name in the response detail
    (much friendlier than the bare ``IntegrityError`` the DB UNIQUE would
    surface).
    """

    def __init__(self, country_code: str, other_zone_id: int, other_zone_name: str) -> None:
        self.country_code = country_code
        self.other_zone_id = other_zone_id
        self.other_zone_name = other_zone_name
        super().__init__(
            f"Country '{country_code}' is already in zone '{other_zone_name}' (id={other_zone_id})"
        )


class ShippingRateUnavailableError(Exception):
    """No rate exists for the asked ``(country_code, currency, method_id)`` triple.

    Raised at order-creation time when a customer's chosen shipping method
    isn't priced for their country/currency. The orders route maps it to a
    422 — the FE should never let this happen because the selector only
    offers methods returned by the quote endpoint, but the backend revalidates
    on the wire.
    """

    def __init__(self, *, country_code: str, currency: str, method_id: int) -> None:
        self.country_code = country_code
        self.currency = currency
        self.method_id = method_id
        super().__init__(
            f"No shipping rate available for country={country_code} "
            f"currency={currency} method_id={method_id}"
        )


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------


class CRUDShippingZone(CRUDBase[ShippingZone, ShippingZoneCreate, ShippingZoneUpdate]):
    """Zone CRUD with country-set management.

    Country assignment is replace-semantics: callers pass the full desired
    code list and the diff (add/remove) is computed here so the route stays
    declarative.
    """

    def get_with_countries(self, db: Session, id: int) -> ShippingZone | None:
        return (
            db.query(ShippingZone)
            .options(selectinload(ShippingZone.countries))
            .filter(ShippingZone.id == id)
            .first()
        )

    def get_multi_admin(self, db: Session, *, skip: int = 0, limit: int = 50) -> list[ShippingZone]:
        """List zones eager-loading the children needed for the list row counts."""
        return (
            db.query(ShippingZone)
            .options(selectinload(ShippingZone.countries), selectinload(ShippingZone.rates))
            .order_by(ShippingZone.sort_order, ShippingZone.name)
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_admin(self, db: Session) -> int:
        return db.query(ShippingZone).count()

    def set_countries(self, db: Session, *, zone: ShippingZone, codes: list[str]) -> ShippingZone:
        """Replace the zone's country set with the canonical ``codes`` list.

        ISO-2 codes are uppercased before comparison (the Pydantic schema
        already constrains to ``^[A-Z]{2}$``, but a stray lowercase byte
        from a manual API caller still gets normalised here). Raises
        ``ShippingZoneCountryConflictError`` when a code is already assigned
        to a different zone (looking up the other zone's name for a clean
        admin-facing message).
        """
        target = [c.upper() for c in codes]
        existing = {c.country_code: c for c in zone.countries}

        to_remove = [c for code, c in existing.items() if code not in target]
        to_add = [code for code in target if code not in existing]

        # Pre-flight check: are any to_add codes assigned to another zone?
        if to_add:
            other = (
                db.query(ShippingZoneCountry)
                .options(joinedload(ShippingZoneCountry.zone))
                .filter(
                    ShippingZoneCountry.country_code.in_(to_add),
                    ShippingZoneCountry.zone_id != zone.id,
                )
                .first()
            )
            if other is not None:
                raise ShippingZoneCountryConflictError(
                    country_code=other.country_code,
                    other_zone_id=other.zone_id,
                    other_zone_name=other.zone.name,
                )

        for row in to_remove:
            db.delete(row)
        for code in to_add:
            db.add(ShippingZoneCountry(zone_id=zone.id, country_code=code))

        db.flush()
        db.refresh(zone)
        return zone

    def create_with_countries(self, db: Session, *, obj_in: ShippingZoneCreate) -> ShippingZone:
        """Create a zone and assign its initial country set atomically."""
        zone = ShippingZone(
            name=obj_in.name,
            description=obj_in.description,
            is_active=obj_in.is_active,
            sort_order=obj_in.sort_order,
        )
        db.add(zone)
        db.flush()
        if obj_in.country_codes:
            self.set_countries(db, zone=zone, codes=obj_in.country_codes)
        return zone

    def apply_update(
        self, db: Session, *, zone: ShippingZone, obj_in: ShippingZoneUpdate
    ) -> ShippingZone:
        """Apply a partial update; reassign countries if provided.

        Mirrors the orders ``apply_admin_update`` pattern: only touches
        fields present in ``model_fields_set`` so a partial PATCH that
        omits ``country_codes`` keeps the existing assignment.
        """
        data = obj_in.model_dump(exclude_unset=True)
        for field in ("name", "description", "is_active", "sort_order"):
            if field in data:
                setattr(zone, field, data[field])
        if "country_codes" in data and data["country_codes"] is not None:
            self.set_countries(db, zone=zone, codes=data["country_codes"])
        db.add(zone)
        return zone

    def country_codes_for(self, zone: ShippingZone) -> list[str]:
        """Sorted code list for response serialization (UI displays alphabetical)."""
        return sorted(c.country_code for c in zone.countries)


# ---------------------------------------------------------------------------
# Methods
# ---------------------------------------------------------------------------


class CRUDShippingMethod(CRUDBase[ShippingMethod, ShippingMethodCreate, ShippingMethodUpdate]):
    def get_active(self, db: Session) -> list[ShippingMethod]:
        return (
            db.query(ShippingMethod)
            .filter(ShippingMethod.is_active.is_(True))
            .order_by(ShippingMethod.sort_order, ShippingMethod.name)
            .all()
        )

    def get_multi_admin(
        self, db: Session, *, skip: int = 0, limit: int = 50
    ) -> list[ShippingMethod]:
        return (
            db.query(ShippingMethod)
            .order_by(ShippingMethod.sort_order, ShippingMethod.name)
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_admin(self, db: Session) -> int:
        return db.query(ShippingMethod).count()


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------


class CRUDShippingRate(CRUDBase[ShippingRate, ShippingRateCreate, ShippingRateUpdate]):
    """Rate CRUD + the single resolver entry point (``apply_shipping_rate``).

    The resolver lives here (not on the model) so it can be unit-tested
    against frozen Pydantic tiers without touching SQLAlchemy.
    """

    # --- list/filter ---

    def get_multi_admin(
        self,
        db: Session,
        *,
        zone_id: int | None = None,
        method_id: int | None = None,
        currency: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[ShippingRate]:
        query = db.query(ShippingRate).options(
            joinedload(ShippingRate.zone), joinedload(ShippingRate.method)
        )
        if zone_id is not None:
            query = query.filter(ShippingRate.zone_id == zone_id)
        if method_id is not None:
            query = query.filter(ShippingRate.method_id == method_id)
        if currency is not None:
            query = query.filter(ShippingRate.currency == currency.upper())
        return (
            query.order_by(ShippingRate.zone_id, ShippingRate.method_id, ShippingRate.currency)
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_admin(
        self,
        db: Session,
        *,
        zone_id: int | None = None,
        method_id: int | None = None,
        currency: str | None = None,
    ) -> int:
        query = db.query(ShippingRate)
        if zone_id is not None:
            query = query.filter(ShippingRate.zone_id == zone_id)
        if method_id is not None:
            query = query.filter(ShippingRate.method_id == method_id)
        if currency is not None:
            query = query.filter(ShippingRate.currency == currency.upper())
        return query.count()

    # --- mutate ---

    def create(self, db: Session, *, obj_in: ShippingRateCreate) -> ShippingRate:
        rate = ShippingRate(
            zone_id=obj_in.zone_id,
            method_id=obj_in.method_id,
            currency=obj_in.currency,
            amount=obj_in.amount,
            free_above_subtotal=obj_in.free_above_subtotal,
            tiers=self._serialise_tiers(obj_in.tiers),
        )
        db.add(rate)
        db.flush()
        db.refresh(rate)
        return rate

    def apply_update(
        self, db: Session, *, rate: ShippingRate, obj_in: ShippingRateUpdate
    ) -> ShippingRate:
        data = obj_in.model_dump(exclude_unset=True)
        for field in ("currency", "amount", "free_above_subtotal"):
            if field in data:
                setattr(rate, field, data[field])
        if "tiers" in data:
            rate.tiers = self._serialise_tiers(obj_in.tiers)
        db.add(rate)
        return rate

    # --- resolve ---

    def get_for_combo(
        self, db: Session, *, zone_id: int, method_id: int, currency: str
    ) -> ShippingRate | None:
        return (
            db.query(ShippingRate)
            .filter(
                ShippingRate.zone_id == zone_id,
                ShippingRate.method_id == method_id,
                ShippingRate.currency == currency.upper(),
            )
            .first()
        )

    def resolve_for_country(
        self, db: Session, *, country_code: str, currency: str
    ) -> list[tuple[ShippingMethod, ShippingRate]]:
        """All (method, rate) pairs available for the asked country+currency.

        Joins ``shipping_zone_countries`` → ``shipping_zones`` → ``shipping_rates``
        → ``shipping_methods`` in one query; filters to active methods + active
        zones; orders by ``method.sort_order, method.name`` for a stable selector
        layout.
        """
        rows = (
            db.query(ShippingMethod, ShippingRate)
            .join(ShippingRate, ShippingRate.method_id == ShippingMethod.id)
            .join(ShippingZone, ShippingRate.zone_id == ShippingZone.id)
            .join(
                ShippingZoneCountry,
                ShippingZoneCountry.zone_id == ShippingZone.id,
            )
            .filter(
                ShippingZoneCountry.country_code == country_code.upper(),
                ShippingRate.currency == currency.upper(),
                ShippingMethod.is_active.is_(True),
                ShippingZone.is_active.is_(True),
            )
            .order_by(ShippingMethod.sort_order, ShippingMethod.name)
            .all()
        )
        return [(method, rate) for method, rate in rows]

    def resolve_one(
        self,
        db: Session,
        *,
        country_code: str,
        currency: str,
        method_id: int,
    ) -> tuple[ShippingMethod, ShippingRate] | None:
        """Resolve a specific (country, currency, method) — used at order create.

        Returns ``None`` if the method isn't available for the country+currency
        (the order route maps this to a 422 via ``ShippingRateUnavailableError``).
        """
        row = (
            db.query(ShippingMethod, ShippingRate)
            .join(ShippingRate, ShippingRate.method_id == ShippingMethod.id)
            .join(ShippingZone, ShippingRate.zone_id == ShippingZone.id)
            .join(
                ShippingZoneCountry,
                ShippingZoneCountry.zone_id == ShippingZone.id,
            )
            .filter(
                ShippingMethod.id == method_id,
                ShippingZoneCountry.country_code == country_code.upper(),
                ShippingRate.currency == currency.upper(),
                ShippingMethod.is_active.is_(True),
                ShippingZone.is_active.is_(True),
            )
            .first()
        )
        if row is None:
            return None
        # `.first()` on a multi-entity Query returns a Row; unpack to a plain
        # tuple to match the declared return type (and so the order CRUD's
        # ``method, rate = …`` destructure works the same way as the
        # ``resolve_for_country`` listing path).
        method, rate = row
        return (method, rate)

    @staticmethod
    def apply_shipping_rate(rate: ShippingRate, subtotal: float) -> float:
        """Pure resolver: free-above → tiers → flat amount.

        - Free-above wins first (the cleanest "free over X" customer promise).
        - Tiers next: the band with the largest ``min_subtotal <= subtotal``.
        - Amount last (the always-set fallback / flat case).

        ``rate.tiers`` is the JSONB column; when set it's a list of dicts
        ``[{"min_subtotal": ..., "amount": ...}, ...]`` already sorted ascending
        (the CRUD canonicalises on insert/update). When unset/empty the tiered
        branch is skipped without sorting.
        """
        if rate.free_above_subtotal is not None and subtotal >= rate.free_above_subtotal:
            return 0.0
        tiers = rate.tiers
        if tiers:
            chosen_amount: float | None = None
            for band in tiers:
                threshold = float(band["min_subtotal"])
                if threshold <= subtotal:
                    chosen_amount = float(band["amount"])
                else:
                    # tiers are sorted ascending — once we pass the subtotal,
                    # later bands can't match. Short-circuit.
                    break
            if chosen_amount is not None:
                return chosen_amount
        return rate.amount

    @staticmethod
    def _serialise_tiers(
        tiers: list[ShippingRateTier] | None,
    ) -> list[dict[str, Any]] | None:
        """Canonicalise tiers for JSONB storage: sorted ascending, plain dicts.

        Pydantic delivers ``ShippingRateTier`` model instances; the JSONB
        column wants plain dicts. The sort is deterministic so the resolver
        can short-circuit on the first ``threshold > subtotal``.
        """
        if not tiers:
            return None
        return [
            {"min_subtotal": t.min_subtotal, "amount": t.amount}
            for t in sorted(tiers, key=lambda b: b.min_subtotal)
        ]

    # --- list-row helpers ---

    def aggregate_country_counts(self, db: Session) -> dict[int, int]:
        """Map zone_id → number of assigned countries.

        Used by the admin zone list to render the count column without an
        N+1. One grouped query.
        """
        rows = (
            db.query(ShippingZoneCountry.zone_id, func.count(ShippingZoneCountry.id))
            .group_by(ShippingZoneCountry.zone_id)
            .all()
        )
        return {zone_id: count for zone_id, count in rows}

    def aggregate_rate_counts(self, db: Session) -> dict[int, int]:
        """Map zone_id -> rate count (across methods x currencies)."""
        rows = (
            db.query(ShippingRate.zone_id, func.count(ShippingRate.id))
            .group_by(ShippingRate.zone_id)
            .all()
        )
        return {zone_id: count for zone_id, count in rows}

    def country_in_any_zone(self, db: Session, *, country_code: str) -> bool:
        """Return True if the country belongs to any zone (used by the quote 'we
        don't ship here' detection — empty methods is the signal)."""
        return (
            db.query(
                exists().where(ShippingZoneCountry.country_code == country_code.upper())
            ).scalar()
            or False
        )


# Public re-exports — exposed via app.crud.shipping namespace so callers can
# write `from app.crud.shipping import zone, method, rate` (the singleton
# convention used by every other CRUD module).
zone = CRUDShippingZone(ShippingZone)
method = CRUDShippingMethod(ShippingMethod)
rate = CRUDShippingRate(ShippingRate)
