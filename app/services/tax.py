"""Rate resolution: ``(tax category, country, date) → rate``.

The one rule worth stating up front: **a missing rate is an error, never 0%.**
Defaulting an unknown country to zero would under-collect VAT silently, and
under-collected VAT is the seller's own liability — it cannot be billed back to
the customer afterwards. So resolution fails loudly and the caller decides.
"""

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tax import TaxCategory, TaxRate

if TYPE_CHECKING:
    from app.models.product import Product


class TaxRateNotFoundError(Exception):
    """No rate covers this (category, country, date).

    Raised rather than falling back to 0% or to the store's standard rate: both
    fallbacks produce a plausible-looking wrong number, which is the failure mode
    tax code most needs to avoid.
    """

    def __init__(self, category_code: str, country_code: str, on_date: date) -> None:
        self.category_code = category_code
        self.country_code = country_code
        self.on_date = on_date
        super().__init__(
            f"No tax rate configured for category '{category_code}' in "
            f"'{country_code}' on {on_date.isoformat()}"
        )


def get_default_category(db: Session) -> TaxCategory:
    """The category products fall back to when they specify none."""
    category = db.scalars(select(TaxCategory).where(TaxCategory.is_default.is_(True))).first()
    if category is None:
        raise LookupError(
            "No default tax category is configured — exactly one tax_categories "
            "row must have is_default = true."
        )
    return category


def category_for_product(db: Session, product: "Product", *, country_code: str) -> TaxCategory:
    """The tax category a product should be taxed under.

    Transitional, and deliberately not a plain "category or default": products
    created *after* the categories migration but *before* the admin UI switches
    to a category picker still carry only ``vat_rate``. Falling straight back to
    the default would tax an 8%-rate product at 23% — silently, and only visible
    on the faktura. So an unclassified product is matched to the category whose
    home-country rate equals its legacy ``vat_rate``, and only a product that
    matches nothing lands on the default.

    Removed once ``product.vat_rate`` is dropped and every product carries a
    category outright.
    """
    if product.tax_category is not None:
        return product.tax_category

    legacy_rate = Decimal(product.vat_rate)
    matched = db.scalars(
        select(TaxCategory)
        .join(TaxRate, TaxRate.tax_category_id == TaxCategory.id)
        .where(
            TaxRate.country_code == country_code,
            TaxRate.rate_pct == legacy_rate,
            TaxRate.valid_to.is_(None),
        )
        .order_by(TaxCategory.id)
    ).first()
    return matched if matched is not None else get_default_category(db)


def resolve_rate(
    db: Session,
    *,
    category: TaxCategory,
    country_code: str,
    on_date: date | None = None,
) -> Decimal:
    """Resolve the VAT rate (percent) for ``category`` in ``country_code``.

    Validity windows are half-open — ``valid_from`` inclusive, ``valid_to``
    exclusive, NULL meaning still in force — so the rate that applied on the date
    of an old order is still recoverable when its faktura is reprinted.

    When windows overlap (an admin left a stale one open), the latest
    ``valid_from`` that covers the date wins, which makes resolution deterministic
    and gives "most recently configured" the intuitive meaning.
    """
    when = on_date or date.today()
    rate = db.scalars(
        select(TaxRate)
        .where(
            TaxRate.tax_category_id == category.id,
            TaxRate.country_code == country_code,
            TaxRate.valid_from <= when,
            (TaxRate.valid_to.is_(None)) | (TaxRate.valid_to > when),
        )
        .order_by(TaxRate.valid_from.desc())
    ).first()
    if rate is None:
        raise TaxRateNotFoundError(category.code, country_code, when)
    return rate.rate_pct


def resolve_rate_int(
    db: Session,
    *,
    category: TaxCategory,
    country_code: str,
    on_date: date | None = None,
) -> int:
    """``resolve_rate`` narrowed to the integer that order lines snapshot.

    ``order_items.vat_rate`` is an ``Integer`` (every Polish rate is a whole
    number). Fractional rates exist elsewhere in the EU (FR 2.1%, IE 13.5%), so
    this refuses to truncate rather than silently rounding a rate — the day a
    fractional rate is configured, this raises and the snapshot column gets
    widened deliberately.
    """
    rate = resolve_rate(db, category=category, country_code=country_code, on_date=on_date)
    if rate != rate.to_integral_value():
        raise ValueError(
            f"Tax rate {rate}% for '{category.code}' in '{country_code}' is fractional, but "
            "order_items.vat_rate is an integer column. Widen the snapshot column (and the "
            "faktura VAT-group rendering) before configuring fractional rates."
        )
    return int(rate)
