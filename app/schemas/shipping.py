"""Shipping zones, methods, and rates Pydantic schemas."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.schemas.base import BaseSchema, TimestampSchema

# ISO-2 uppercase country code (two letters A-Z). Used on the wire — clients
# must send the canonical uppercase form.
ISO2Country = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$", min_length=2, max_length=2)]

# Currency code from the supported triple. Mirrors the DB CHECK constraint on
# shipping_rates.currency so a bad value 422s before SQL.
ShippingCurrency = Literal["USD", "PLN", "EUR"]

# Pricing-shape classifier returned on list/quote rows so the FE can badge or
# annotate without re-deriving from the row's nullable columns. ``weight_tiered``
# takes precedence over ``tiered`` (subtotal) because weight is the primary
# signal in the resolver — a rate with both weight and subtotal tiers is
# classified by its weight shape.
PricingKind = Literal["flat", "free_above", "tiered", "weight_tiered"]


# ---------------------------------------------------------------------------
# Rate tier bands
# ---------------------------------------------------------------------------


class ShippingRateTier(BaseSchema):
    """One band in a tiered subtotal-priced shipping rate.

    The resolver picks the band whose ``min_subtotal`` is the largest value
    ``<= subtotal``. Bands are stored sorted ascending by ``min_subtotal``;
    the CRUD's ``sort_tiers`` canonicalises before persistence so the resolver
    never has to sort on every quote.
    """

    min_subtotal: float = Field(..., ge=0)
    amount: float = Field(..., ge=0)


class ShippingRateWeightTier(BaseSchema):
    """One band in a weight-priced shipping rate.

    The resolver picks the cheapest band whose ``max_weight_kg`` covers the
    cart's total weight (smallest ``max_weight_kg`` such that
    ``max_weight_kg >= cart_weight``). Bands are stored sorted ascending by
    ``max_weight_kg``; the CRUD canonicalises before persistence.

    Why ``max_weight_kg`` (not ``min_weight_kg``): courier price-lists are
    universally "up to X kg = $Y," so the schema mirrors how admins think.
    """

    max_weight_kg: float = Field(..., gt=0)
    amount: float = Field(..., ge=0)


def _validate_tiers(tiers: list[ShippingRateTier] | None) -> list[ShippingRateTier] | None:
    """Reject duplicate ``min_subtotal`` values in a tier list.

    Two bands sharing the same threshold would make the resolver's choice
    non-deterministic (it picks "the largest <= subtotal" — ties have no
    natural winner). Empty lists are normalised to ``None`` so the resolver's
    "tiers set?" check short-circuits cleanly.
    """
    if tiers is None or not tiers:
        return None
    thresholds = [t.min_subtotal for t in tiers]
    if len(set(thresholds)) != len(thresholds):
        raise ValueError("Tier bands must have unique min_subtotal values")
    return tiers


def _validate_weight_tiers(
    tiers: list[ShippingRateWeightTier] | None,
) -> list[ShippingRateWeightTier] | None:
    """Reject duplicate ``max_weight_kg`` values in a weight-tier list.

    Two bands sharing the same ``max_weight_kg`` would make the resolver's
    "smallest covering band" choice non-deterministic. Empty lists are
    normalised to ``None`` so the resolver's ``if weight_tiers`` short-circuit
    is clean.
    """
    if tiers is None or not tiers:
        return None
    thresholds = [t.max_weight_kg for t in tiers]
    if len(set(thresholds)) != len(thresholds):
        raise ValueError("Weight-tier bands must have unique max_weight_kg values")
    return tiers


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------


class ShippingZoneBase(BaseSchema):
    name: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    description: str | None = None
    is_active: bool = True
    sort_order: int = 0
    # Volumetric weight divisor (cm³/kg) for this zone. PL/EU ground ≈ 5000,
    # international air ≈ 6000. NULL = no volumetric pricing for the zone
    # (resolver bills actual weight only). ``gt=0`` mirrors the
    # ck_shipping_zones_dim_divisor_positive CHECK so a bad value 422s before
    # SQL.
    dim_divisor: float | None = Field(default=None, gt=0)


class ShippingZoneCreate(ShippingZoneBase):
    country_codes: list[ISO2Country] = Field(default_factory=list)

    @field_validator("country_codes")
    @classmethod
    def _unique_codes(cls, codes: list[str]) -> list[str]:
        """Reject duplicate codes in the request body (cleaner than a DB error)."""
        if len(set(codes)) != len(codes):
            raise ValueError("Duplicate country codes in country_codes")
        return codes


class ShippingZoneUpdate(BaseSchema):
    name: Annotated[str, StringConstraints(min_length=1, max_length=80)] | None = None
    description: str | None = None
    is_active: bool | None = None
    sort_order: int | None = None
    country_codes: list[ISO2Country] | None = None
    dim_divisor: float | None = Field(default=None, gt=0)

    @field_validator("country_codes")
    @classmethod
    def _unique_codes(cls, codes: list[str] | None) -> list[str] | None:
        if codes is None:
            return None
        if len(set(codes)) != len(codes):
            raise ValueError("Duplicate country codes in country_codes")
        return codes


class ShippingZoneListItem(TimestampSchema):
    name: str
    description: str | None
    is_active: bool
    sort_order: int
    country_count: int
    rate_count: int
    dim_divisor: float | None


class ShippingZoneListResponse(BaseSchema):
    items: list[ShippingZoneListItem]
    total: int
    page: int
    page_size: int
    pages: int


class ShippingZoneDetail(TimestampSchema):
    name: str
    description: str | None
    is_active: bool
    sort_order: int
    country_codes: list[str]
    dim_divisor: float | None


# ---------------------------------------------------------------------------
# Methods
# ---------------------------------------------------------------------------


class ShippingMethodBase(BaseSchema):
    name: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    code: Annotated[str, StringConstraints(min_length=1, max_length=40)] | None = None
    description: str | None = None
    sort_order: int = 0
    is_active: bool = True


class ShippingMethodCreate(ShippingMethodBase):
    pass


class ShippingMethodUpdate(BaseSchema):
    name: Annotated[str, StringConstraints(min_length=1, max_length=80)] | None = None
    code: Annotated[str, StringConstraints(min_length=1, max_length=40)] | None = None
    description: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class ShippingMethodResponse(TimestampSchema):
    name: str
    code: str | None
    description: str | None
    sort_order: int
    is_active: bool


class ShippingMethodListResponse(BaseSchema):
    items: list[ShippingMethodResponse]
    total: int
    page: int
    page_size: int
    pages: int


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------


class ShippingRateBase(BaseSchema):
    currency: ShippingCurrency
    amount: float = Field(..., ge=0)
    free_above_subtotal: float | None = Field(default=None, ge=0)
    tiers: list[ShippingRateTier] | None = None
    weight_tiers: list[ShippingRateWeightTier] | None = None

    @model_validator(mode="after")
    def _normalise_tiers(self) -> ShippingRateBase:
        self.tiers = _validate_tiers(self.tiers)
        self.weight_tiers = _validate_weight_tiers(self.weight_tiers)
        return self


class ShippingRateCreate(ShippingRateBase):
    zone_id: int
    method_id: int


class ShippingRateUpdate(BaseSchema):
    currency: ShippingCurrency | None = None
    amount: float | None = Field(default=None, ge=0)
    free_above_subtotal: float | None = Field(default=None, ge=0)
    tiers: list[ShippingRateTier] | None = None
    weight_tiers: list[ShippingRateWeightTier] | None = None

    @model_validator(mode="after")
    def _normalise_tiers(self) -> ShippingRateUpdate:
        if "tiers" in self.model_fields_set:
            self.tiers = _validate_tiers(self.tiers)
        if "weight_tiers" in self.model_fields_set:
            self.weight_tiers = _validate_weight_tiers(self.weight_tiers)
        return self


def _classify_pricing(
    amount: Decimal | float,
    free_above_subtotal: Decimal | float | None,
    tiers: list[ShippingRateTier] | list[dict] | None,
    weight_tiers: list[ShippingRateWeightTier] | list[dict] | None = None,
) -> PricingKind:
    """Return a single tag for the rate's pricing shape (used by list + quote rows).

    ``weight_tiered`` wins over ``tiered`` (subtotal) because weight is the
    primary signal in the resolver precedence (see ``apply_shipping_rate``).
    """
    if weight_tiers:
        return "weight_tiered"
    if tiers:
        return "tiered"
    if free_above_subtotal is not None:
        return "free_above"
    return "flat"


class ShippingRateListItem(TimestampSchema):
    zone_id: int
    zone_name: str
    method_id: int
    method_name: str
    currency: str
    amount: float
    free_above_subtotal: float | None
    tiers: list[ShippingRateTier] | None
    weight_tiers: list[ShippingRateWeightTier] | None
    pricing_kind: PricingKind


class ShippingRateListResponse(BaseSchema):
    items: list[ShippingRateListItem]
    total: int
    page: int
    page_size: int
    pages: int


class ShippingRateResponse(TimestampSchema):
    zone_id: int
    method_id: int
    currency: str
    amount: float
    free_above_subtotal: float | None
    tiers: list[ShippingRateTier] | None
    weight_tiers: list[ShippingRateWeightTier] | None


# ---------------------------------------------------------------------------
# Public quote
# ---------------------------------------------------------------------------


class ShippingQuoteMethod(BaseSchema):
    """One method available for the asked ``(country, currency, subtotal)``.

    ``amount`` is the **resolved** shipping price (free-above + tiers already
    applied) — the storefront renders it directly without re-running the
    resolver. ``is_free`` flags the free-above-triggered case so the UI can
    badge it. ``pricing_kind`` exposes the rate's shape so we can drop a
    "rates vary by cart total" hint in the future without a backend change.
    """

    id: int  # method id
    name: str
    code: str | None
    description: str | None
    sort_order: int
    amount: float
    currency: ShippingCurrency
    is_free: bool
    pricing_kind: PricingKind
    free_above_subtotal: float | None


class ShippingQuoteResponse(BaseSchema):
    country_code: ISO2Country
    currency: ShippingCurrency
    subtotal: float
    methods: list[ShippingQuoteMethod]
