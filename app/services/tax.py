"""Rate resolution: ``(tax category, country, date) → rate``.

The one rule worth stating up front: **a missing rate is an error, never 0%.**
Defaulting an unknown country to zero would under-collect VAT silently, and
under-collected VAT is the seller's own liability — it cannot be billed back to
the customer afterwards. So resolution fails loudly and the caller decides.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.countries import EU_MEMBER_STATES
from app.models.tax import TaxCategory, TaxRate

# ``order.tax_treatment`` values. Domestic = today's behaviour (taxed at the home
# rate). Reverse charge = a 0%-rate sale priced at net (intra-community B2B).
TREATMENT_DOMESTIC = "domestic"
TREATMENT_REVERSE_CHARGE = "reverse_charge"


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


def _utcnow() -> datetime:
    """Naive UTC, matching the datetime columns VIES confirmation is stored in."""
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class TaxContext:
    """The tax treatment of one order, resolved once at build time.

    Reverse charge is modelled as *a 0%-rate sale priced at net* — the buyer pays
    the net (VAT-stripped) price and the store charges no VAT (the buyer
    self-accounts in their own member state). ``buyer_vat_id`` /
    ``buyer_vies_confirmed_at`` are the audit evidence that gets snapshotted onto
    the order, so it can never be re-derived from a mutable company row.
    """

    treatment: str = TREATMENT_DOMESTIC
    buyer_vat_id: str | None = None
    buyer_vies_confirmed_at: datetime | None = None

    @property
    def is_reverse_charge(self) -> bool:
        return self.treatment == TREATMENT_REVERSE_CHARGE

    @classmethod
    def domestic(cls) -> "TaxContext":
        return cls(treatment=TREATMENT_DOMESTIC)

    @classmethod
    def from_order(cls, order) -> "TaxContext":
        """Rebuild the context an order was placed under, from its snapshot.

        Used by the order-edit recompute path so re-pricing a placed order keeps
        its original treatment rather than re-resolving (which could flip it if
        the company's confirmation has since lapsed)."""
        return cls(
            treatment=order.tax_treatment,
            buyer_vat_id=order.buyer_vat_id,
            buyer_vies_confirmed_at=order.buyer_vies_confirmed_at,
        )


def resolve_tax_context(
    db: Session, *, user_id: int | None, shipping_country: str | None
) -> TaxContext:
    """Decide DOMESTIC vs REVERSE_CHARGE for an order being placed.

    Reverse charge applies iff **all** hold (anything else ⇒ domestic, i.e. today's
    behaviour — the fail-closed default):

    1. the destination is another EU member state (``shipping_country`` ∈ EU,
       ``≠`` the home country — intra-community supply needs transport out), and
    2. the buyer is an active member of a company, and
    3. that company has a VIES-**confirmed** VAT-ID whose confirmation is still
       within the freshness window (``VIES_CONFIRMATION_TTL_DAYS``).

    A stale or missing confirmation charges home VAT — under-charging VAT is the
    seller's liability, so the safe default is to charge it.
    """
    if user_id is None or shipping_country is None:
        return TaxContext.domestic()
    if shipping_country == settings.TAX_HOME_COUNTRY or shipping_country not in EU_MEMBER_STATES:
        return TaxContext.domestic()

    # Imported here (not at module scope) to avoid a services↔crud import cycle.
    from app.crud import company_user as crud_company_user

    membership = crud_company_user.get_active_membership(db, user_id=user_id)
    if membership is None:
        return TaxContext.domestic()
    company = membership.company
    if not company.vies_valid or company.vat_id is None or company.vies_confirmed_at is None:
        return TaxContext.domestic()
    ttl = timedelta(days=settings.VIES_CONFIRMATION_TTL_DAYS)
    if _utcnow() - company.vies_confirmed_at > ttl:
        return TaxContext.domestic()  # confirmation lapsed → fail closed
    return TaxContext(
        treatment=TREATMENT_REVERSE_CHARGE,
        buyer_vat_id=company.vat_id,
        buyer_vies_confirmed_at=company.vies_confirmed_at,
    )
