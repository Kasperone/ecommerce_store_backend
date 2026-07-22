"""Tax-category and tax-rate schemas — the admin-managed tax vocabulary.

A **category** is a kind of goods ("standard", "books"); a **rate** is what that
category attracts in one country over one validity window. Products are
classified by category (``product.tax_category_id``); the rate is resolved from
``(category, destination country, date)`` at order time — never stored on the
product. See ``app/models/tax.py`` and ``app/services/tax.py``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import Field, model_validator

from app.core.countries import CountryCode
from app.schemas.base import BaseSchema, TimestampSchema

# Numeric(5,2) in the DB — FR 2.1% / IE 13.5% exist, so rates are not integers.
RatePct = Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]


class TaxRateBase(BaseSchema):
    """Shared rate fields (create + response), minus the parent category id.

    Validity windows are half-open: ``valid_from`` inclusive, ``valid_to``
    exclusive, NULL = still in force.
    """

    country_code: CountryCode
    rate_pct: RatePct
    valid_from: date
    valid_to: date | None = None

    @model_validator(mode="after")
    def _window_ordered(self) -> TaxRateBase:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self


class TaxRateCreate(TaxRateBase):
    """Create body for a rate. The category is taken from the path, not here."""


class TaxRateUpdate(BaseSchema):
    """Partial rate update. The parent category is immutable — move a rate by
    deleting and recreating it under another category."""

    country_code: CountryCode | None = None
    rate_pct: RatePct | None = None
    valid_from: date | None = None
    valid_to: date | None = None


class TaxRateResponse(TaxRateBase, TimestampSchema):
    """A rate with its id, parent category, and timestamps."""

    id: int
    tax_category_id: int


class TaxCategoryBase(BaseSchema):
    """Shared category fields (create + response)."""

    code: Annotated[str, Field(min_length=1, max_length=32)]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    name_pl: Annotated[str | None, Field(max_length=100)] = None
    is_default: bool = False


class TaxCategoryCreate(TaxCategoryBase):
    """Create body. Setting ``is_default`` unsets the previous default."""


class TaxCategoryUpdate(BaseSchema):
    """Partial category update. ``code`` is immutable once set — imports,
    exports, and the migration backfill reference it by value."""

    name: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    name_pl: Annotated[str | None, Field(max_length=100)] = None
    is_default: bool | None = None


class TaxCategoryResponse(TaxCategoryBase, TimestampSchema):
    """A category with its id, timestamps, and its rates nested.

    The rates ride along so the admin picker can label each category with its
    home-country rate in a single fetch.
    """

    id: int
    rates: list[TaxRateResponse] = Field(default_factory=list)
