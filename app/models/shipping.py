"""Shipping zones, methods, and rates.

The model is a three-level normalization:

- ``ShippingZone`` groups a set of ISO-2 country codes
  (``ShippingZoneCountry``). A country belongs to **at most one** zone — a
  ``UNIQUE`` on ``country_code`` enforces this at the DB level so the
  resolver never has to disambiguate.
- ``ShippingMethod`` is a named delivery service (``Standard``,
  ``Express``, ``Kurier DPD``, ``Paczkomat InPost``, …). Methods are
  carrier-agnostic at this layer; courier-API integration is a separate
  future epic.
- ``ShippingRate`` is the price for one ``(zone, method, currency)``
  triple. ``UNIQUE(zone_id, method_id, currency)`` prevents two rows for
  the same combo.

Rates carry three pricing shapes on one row so admins can grow without a
schema change:

- ``amount`` (required): the base/fallback shipping price.
- ``free_above_subtotal`` (optional): when set and the cart subtotal in
  the same currency reaches it, shipping is zero (admins' "free over X"
  promise).
- ``tiers`` (optional, JSONB): a list of ``{min_subtotal, amount}``
  bands sorted ascending. When set, the resolver picks the band whose
  ``min_subtotal`` is the largest value ``<= subtotal``. Lets admins
  price subtotal-banded rates without touching the schema.

Resolver precedence (``crud_shipping_rate.apply_shipping_rate``):
``free_above`` triggers first → ``tiers`` next → ``amount`` last. A
global "free over X" trumps tier discounts because it's the cleanest
customer-facing promise.

Weight-based tiers (``ShippingRate.weight_tiers``) layer on top of
``Product.weight_kg`` (added 2026-05-30). When set, the resolver picks
the cheapest band whose ``max_weight_kg`` covers the cart's total weight;
weight resolution slots between ``free_above_subtotal`` and the subtotal
``tiers`` so an admin who configures both gets weight-driven pricing as
the primary signal (PL-market couriers price by weight first). When the
cart's total weight exceeds every band, resolution falls through to the
subtotal-tier / flat branches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    pass


class ShippingZone(BaseModel):
    """A named geographic group of countries that share rate scaffolding."""

    __tablename__ = "shipping_zones"

    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    countries: Mapped[list[ShippingZoneCountry]] = relationship(
        "ShippingZoneCountry",
        back_populates="zone",
        cascade="all, delete-orphan",
        order_by="ShippingZoneCountry.country_code",
    )
    rates: Mapped[list[ShippingRate]] = relationship(
        "ShippingRate",
        back_populates="zone",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<ShippingZone {self.name}>"


class ShippingZoneCountry(BaseModel):
    """ISO-2 country → zone mapping.

    ``country_code`` is globally unique so a checkout has exactly one rate
    matrix for any given country (no zone ambiguity at the resolver).
    """

    __tablename__ = "shipping_zone_countries"
    __table_args__ = (
        CheckConstraint(
            "country_code ~ '^[A-Z]{2}$'",
            name="ck_shipping_zone_countries_iso2",
        ),
    )

    zone_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("shipping_zones.id", ondelete="CASCADE"), index=True
    )
    country_code: Mapped[str] = mapped_column(String(2), unique=True, index=True)

    zone: Mapped[ShippingZone] = relationship("ShippingZone", back_populates="countries")

    def __repr__(self) -> str:
        return f"<ShippingZoneCountry {self.country_code} → zone#{self.zone_id}>"


class ShippingMethod(BaseModel):
    """A named delivery service (Standard, Express, Paczkomat, DPD, …).

    Methods are admin-defined and carrier-agnostic at this layer. The
    optional ``code`` lets admins encode a carrier identifier
    (``inpost_paczkomat``, ``dpd_classic``) that downstream integrations
    can match on; the storefront uses ``name`` for display.
    """

    __tablename__ = "shipping_methods"

    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    code: Mapped[str | None] = mapped_column(String(40), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    rates: Mapped[list[ShippingRate]] = relationship(
        "ShippingRate",
        back_populates="method",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<ShippingMethod {self.name}>"


class ShippingRate(BaseModel):
    """Price for one ``(zone, method, currency)`` triple.

    Carries three pricing shapes on one row (see module docstring): the
    required ``amount`` (flat fallback), the optional
    ``free_above_subtotal`` zero-out, and the optional ``tiers`` JSONB
    list of subtotal bands.
    """

    __tablename__ = "shipping_rates"
    __table_args__ = (
        UniqueConstraint(
            "zone_id",
            "method_id",
            "currency",
            name="uq_shipping_rates_zone_method_currency",
        ),
        CheckConstraint(
            "currency IN ('USD', 'PLN', 'EUR')",
            name="ck_shipping_rates_currency",
        ),
        CheckConstraint("amount >= 0", name="ck_shipping_rates_amount_nonneg"),
        CheckConstraint(
            "free_above_subtotal IS NULL OR free_above_subtotal >= 0",
            name="ck_shipping_rates_free_above_nonneg",
        ),
    )

    zone_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("shipping_zones.id", ondelete="CASCADE"), index=True
    )
    method_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("shipping_methods.id", ondelete="CASCADE"), index=True
    )
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[float] = mapped_column(Float)
    free_above_subtotal: Mapped[float | None] = mapped_column(Float)
    # Tiers shape: [{"min_subtotal": 0, "amount": 19.99}, {"min_subtotal": 200, "amount": 14.99}]
    # Stored sorted ascending by min_subtotal (the CRUD canonicalises on insert/update).
    # Pydantic enforces each band's non-negativity + uniqueness of min_subtotal.
    tiers: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    # Weight tiers shape:
    #   [{"max_weight_kg": 1.0, "amount": 9.99}, {"max_weight_kg": 5.0, "amount": 19.99}]
    # Stored sorted ascending by max_weight_kg (the CRUD canonicalises on insert/update).
    # Resolver picks the cheapest band whose max_weight_kg >= cart_weight.
    # When cart_weight > the largest band, falls through to subtotal-tiers / flat.
    weight_tiers: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)

    zone: Mapped[ShippingZone] = relationship("ShippingZone", back_populates="rates")
    method: Mapped[ShippingMethod] = relationship("ShippingMethod", back_populates="rates")

    def __repr__(self) -> str:
        return (
            f"<ShippingRate zone#{self.zone_id} method#{self.method_id} "
            f"{self.currency}={self.amount}>"
        )
