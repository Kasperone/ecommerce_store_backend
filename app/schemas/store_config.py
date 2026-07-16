"""Pydantic schemas for admin store-settings management (single-row config).

The store-identity + VAT values are a single row (``id=1``). There is no create
(the migration seeds it) and no delete — only a response shape and a partial
update body. ``invoice_vat_rate`` is constrained 0-100 here AND by a DB CHECK so
the route, schema, and DB stay in lockstep.

All text fields are length-bounded to match the model columns. The update body
fields are all optional (partial PATCH); a field omitted from the body is left
untouched (``exclude_unset`` in the CRUD layer).
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import Field, field_validator

from app.schemas.base import BaseSchema, TimestampSchema

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Currencies the store supports (matches the per-currency price columns).
_FX_CURRENCIES = {"USD", "PLN", "EUR"}


def _validate_fx_rates(value: dict[str, float] | None) -> dict[str, float] | None:
    """Keys must be supported ISO-4217 codes; values must be positive numbers.

    Empty ``{}`` is valid (no cross-currency support). Used for B2B net-terms
    cross-currency credit only — never for catalog/display pricing.
    """
    if value is None:
        return value
    for code, rate in value.items():
        if code not in _FX_CURRENCIES:
            raise ValueError(f"fx_rates key '{code}' is not a supported currency (USD/PLN/EUR)")
        if not isinstance(rate, int | float) or rate <= 0:
            raise ValueError(f"fx_rates['{code}'] must be a positive number")
    return value


def _validate_primary_color(value: str | None) -> str | None:
    """Blank stays blank (default theme); otherwise require '#rrggbb', lowercased."""
    if value is None:
        return value
    value = value.strip()
    if value == "":
        return value
    if not _HEX_COLOR_RE.match(value):
        raise ValueError("primary_color must be '#rrggbb' hex or blank")
    return value.lower()


class StoreConfigBase(BaseSchema):
    """Shared store-config fields (response shape)."""

    store_name: Annotated[str, Field(max_length=255)]
    brand_tagline: Annotated[str, Field(max_length=255)]
    email_from_name: Annotated[str, Field(max_length=255)]
    primary_color: Annotated[str, Field(max_length=7)]
    logo_url: Annotated[str, Field(max_length=1024)]
    legal_name: Annotated[str, Field(max_length=255)]
    address_line: Annotated[str, Field(max_length=255)]
    address_line2: Annotated[str, Field(max_length=255)]
    postal_city: Annotated[str, Field(max_length=255)]
    country: Annotated[str, Field(max_length=255)]
    tax_id: Annotated[str, Field(max_length=64)]
    nip: Annotated[str, Field(max_length=64)]
    regon: Annotated[str, Field(max_length=64)]
    krs: Annotated[str, Field(max_length=64)]
    email: Annotated[str, Field(max_length=255)]
    phone: Annotated[str, Field(max_length=64)]
    social_instagram: Annotated[str, Field(max_length=255)]
    social_facebook: Annotated[str, Field(max_length=255)]
    social_tiktok: Annotated[str, Field(max_length=255)]
    social_youtube: Annotated[str, Field(max_length=255)]
    invoice_vat_rate: Annotated[int, Field(ge=0, le=100)]
    # Returns (RMA) eligibility window in days from delivery; 0 disables returns.
    returns_window_days: Annotated[int, Field(ge=0)]
    # Store-wide default low-stock threshold; NULL = no global default (today's
    # behaviour). Applies only where a product's own threshold is NULL.
    low_stock_default_threshold: Annotated[int | None, Field(ge=0)] = None
    # Abandoned-cart recovery emails (ships disabled; the detection job is the
    # external-cron script ``app.scripts.send_abandoned_cart_emails``).
    abandoned_cart_enabled: bool = False
    # Hours of saved-cart inactivity before a cart counts as abandoned (1-720,
    # mirrors the model CHECK).
    abandoned_cart_threshold_hours: Annotated[int, Field(ge=1, le=720)] = 24
    # Gift cards + store credit (stored-value program; ships dark — D7). The
    # toggles gate the redemption UX (check endpoint + checkout fields), not
    # reads: the account store-credit page stays available regardless.
    gift_cards_enabled: bool = False
    store_credit_enabled: bool = False
    # B2B cross-currency net-terms credit FX rates (Program D). Maps each
    # supported currency to the value of 1 unit in a common pivot (e.g.
    # USD-relative). Empty {} = no cross-currency support. Used ONLY to convert
    # net-terms orders into a company's credit-limit currency.
    fx_rates: dict[str, float] = Field(default_factory=dict)


class StoreConfigUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/admin/settings`` (all fields optional).

    Only the fields explicitly sent are applied. ``invoice_vat_rate`` is bounded
    0-100 (mirrors the model CHECK constraint).
    """

    store_name: Annotated[str | None, Field(max_length=255)] = None
    brand_tagline: Annotated[str | None, Field(max_length=255)] = None
    email_from_name: Annotated[str | None, Field(max_length=255)] = None
    primary_color: Annotated[str | None, Field(max_length=7)] = None
    logo_url: Annotated[str | None, Field(max_length=1024)] = None
    legal_name: Annotated[str | None, Field(max_length=255)] = None
    address_line: Annotated[str | None, Field(max_length=255)] = None
    address_line2: Annotated[str | None, Field(max_length=255)] = None
    postal_city: Annotated[str | None, Field(max_length=255)] = None
    country: Annotated[str | None, Field(max_length=255)] = None
    tax_id: Annotated[str | None, Field(max_length=64)] = None
    nip: Annotated[str | None, Field(max_length=64)] = None
    regon: Annotated[str | None, Field(max_length=64)] = None
    krs: Annotated[str | None, Field(max_length=64)] = None
    email: Annotated[str | None, Field(max_length=255)] = None
    phone: Annotated[str | None, Field(max_length=64)] = None
    social_instagram: Annotated[str | None, Field(max_length=255)] = None
    social_facebook: Annotated[str | None, Field(max_length=255)] = None
    social_tiktok: Annotated[str | None, Field(max_length=255)] = None
    social_youtube: Annotated[str | None, Field(max_length=255)] = None
    invoice_vat_rate: Annotated[int | None, Field(ge=0, le=100)] = None
    returns_window_days: Annotated[int | None, Field(ge=0)] = None
    # Global default low-stock threshold. Send an explicit ``null`` to clear it
    # back to "no global default"; omit the field to leave it unchanged.
    low_stock_default_threshold: Annotated[int | None, Field(ge=0)] = None
    abandoned_cart_enabled: bool | None = None
    abandoned_cart_threshold_hours: Annotated[int | None, Field(ge=1, le=720)] = None
    gift_cards_enabled: bool | None = None
    store_credit_enabled: bool | None = None
    fx_rates: dict[str, float] | None = None

    _validate_primary_color = field_validator("primary_color")(_validate_primary_color)
    _validate_fx_rates = field_validator("fx_rates")(_validate_fx_rates)


class StoreConfigResponse(StoreConfigBase, TimestampSchema):
    """The store-config row as the API returns it (includes ``id`` + timestamps)."""


class StoreConfigPublic(BaseSchema):
    """Slim public projection for the storefront footer (``GET /store-config``).

    Excludes internal-only fields — notably ``invoice_vat_rate`` and the
    audit/timestamp/id noise. ``nip`` is the *public* tax id (legally required in
    the PL/EU footer) so it stays; the generic internal ``tax_id`` is dropped from
    this shape in favour of it.
    """

    store_name: str
    brand_tagline: str
    primary_color: str
    logo_url: str
    legal_name: str
    address_line: str
    address_line2: str
    postal_city: str
    country: str
    nip: str
    regon: str
    krs: str
    email: str
    phone: str
    social_instagram: str
    social_facebook: str
    social_tiktok: str
    social_youtube: str
    # Stored-value toggles — the storefront hides/shows the gift-card checkout
    # input and the store-credit checkbox off these (D7).
    gift_cards_enabled: bool
    store_credit_enabled: bool
