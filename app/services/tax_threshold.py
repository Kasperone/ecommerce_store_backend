"""EU distance-selling €10 000 threshold monitor.

Under the EU VAT Directive (Art. 59c) a small seller whose **cross-border B2C
supplies to other member states** stay below **€10 000/year** charges home-country
VAT on them; once that EU-wide total is exceeded — in the current *or* the
preceding calendar year — the seller must instead charge the destination
country's VAT (registering for the One-Stop-Shop). Below the line the store's
Path-A behaviour (home VAT on everything) is legally correct; above it, it
silently under- or mis-collects.

This module makes the crossing **fire on data rather than on memory**: it sums
the qualifying sales from order rows and reports a Shopify-style tiered status
(not approaching → approaching → threshold met), surfaced on the admin reports
page and the dashboard overview.

What counts toward the threshold:

- **Cross-border B2C only.** Reverse-charge orders (``tax_treatment ==
  reverse_charge``) are intra-community *B2B* supplies — the buyer self-accounts,
  so they never count toward the distance-selling threshold. Domestic PL sales
  don't count either. The qualifying set is therefore ``tax_treatment ==
  domestic`` **and** ``shipping_country ∈ EU \\ {home}``.
- **Net of VAT.** The threshold is defined on the sale value *excluding* VAT, so
  each order's gross is decomposed to net through the existing
  ``compute_vat_breakdown`` (never a re-implementation of the decomposition).
- **Collected sales only**, by the same ``REVENUE_STATUSES`` every other report
  uses (paid/processing/shipped/delivered), attributed by ``created_at``. A
  net-terms order therefore counts once it leaves ``PENDING``; cancelled and
  fully-refunded orders don't count — both consistent with the rest of the
  reporting surface and conservative for a compliance signal.
- **In EUR.** Per-order net is converted from the order currency to EUR via the
  admin-configured FX rates. An order in a currency with no configured EUR rate
  is *surfaced as a count* (``unconvertible_count``), never silently dropped —
  under-counting the threshold is the failure mode this whole module exists to
  prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.countries import EU_MEMBER_STATES
from app.core.money import ZERO_MONEY, quantize_money
from app.crud import store_config as crud_store_config
from app.crud.report import REVENUE_STATUSES
from app.models.order import Order
from app.services.fx import convert_money
from app.services.invoice import compute_vat_breakdown
from app.services.tax import TREATMENT_DOMESTIC

# The EU-wide micro-business distance-selling threshold (Art. 59c VAT Directive).
EU_DISTANCE_SELLING_THRESHOLD_EUR = Decimal("10000.00")

# Shopify-style tiered status. ``APPROACHING`` gives the merchant lead time to
# register for OSS *before* the crossing rather than discovering it afterwards.
STATUS_NOT_APPROACHING = "not_approaching"
STATUS_APPROACHING = "approaching"
STATUS_THRESHOLD_MET = "threshold_met"

# Fraction of the threshold at which the status flips to APPROACHING.
APPROACHING_BAND = Decimal("0.80")


def _status_for(pct: Decimal) -> str:
    if pct >= 1:
        return STATUS_THRESHOLD_MET
    if pct >= APPROACHING_BAND:
        return STATUS_APPROACHING
    return STATUS_NOT_APPROACHING


@dataclass(frozen=True)
class YearThreshold:
    """One calendar year's qualifying total against the threshold."""

    year: int
    total_eur: Decimal
    pct: Decimal  # total_eur / threshold, e.g. Decimal("0.83")
    remaining_eur: Decimal  # max(threshold - total, 0)
    status: str
    unconvertible_count: int  # qualifying orders whose currency had no EUR rate


@dataclass(frozen=True)
class EuDistanceSellingStatus:
    """Threshold status across the current and preceding calendar year."""

    threshold_eur: Decimal
    current: YearThreshold
    prior: YearThreshold

    @property
    def liable(self) -> bool:
        """EU rule: liable if the threshold is met in *either* year."""
        return (
            self.current.status == STATUS_THRESHOLD_MET or self.prior.status == STATUS_THRESHOLD_MET
        )

    @property
    def effective_status(self) -> str:
        """The worse of the two years — what the dashboard nudge should reflect.

        A prior-year crossing keeps the seller liable even if the current year is
        quiet, so the overview must still show ``threshold_met`` in that case.
        """
        order = {
            STATUS_NOT_APPROACHING: 0,
            STATUS_APPROACHING: 1,
            STATUS_THRESHOLD_MET: 2,
        }
        return max(self.current.status, self.prior.status, key=lambda s: order[s])


def _eu_non_home() -> list[str]:
    """EU member states excluding the store's home country (the qualifying dests)."""
    return sorted(EU_MEMBER_STATES - {settings.TAX_HOME_COUNTRY})


def _year_total(db: Session, *, year: int, shipping_vat_rate: int, fx_rates: dict) -> YearThreshold:
    """Sum the qualifying net-of-VAT sales for ``year``, in EUR."""
    start = date(year, 1, 1)
    end = date(year + 1, 1, 1)  # exclusive upper bound
    orders = db.scalars(
        select(Order)
        .where(
            Order.tax_treatment == TREATMENT_DOMESTIC,
            Order.shipping_country.in_(_eu_non_home()),
            Order.status.in_(REVENUE_STATUSES),
            Order.created_at >= start,
            Order.created_at < end,
        )
        .options(selectinload(Order.items))
    ).all()

    total = ZERO_MONEY
    unconvertible = 0
    for order in orders:
        net = quantize_money(
            sum((g.net for g in compute_vat_breakdown(order, shipping_vat_rate)), ZERO_MONEY)
        )
        eur = convert_money(net, order.currency, "EUR", fx_rates)
        if eur is None:
            unconvertible += 1
            continue
        total += eur

    total = quantize_money(total)
    pct = (
        total / EU_DISTANCE_SELLING_THRESHOLD_EUR
        if EU_DISTANCE_SELLING_THRESHOLD_EUR
        else Decimal(0)
    )
    remaining = quantize_money(max(EU_DISTANCE_SELLING_THRESHOLD_EUR - total, ZERO_MONEY))
    return YearThreshold(
        year=year,
        total_eur=total,
        pct=pct,
        remaining_eur=remaining,
        status=_status_for(pct),
        unconvertible_count=unconvertible,
    )


def eu_distance_selling_status(db: Session, *, year: int | None = None) -> EuDistanceSellingStatus:
    """Threshold status for ``year`` (default: current) and the preceding year.

    Both years are returned because liability is triggered by a crossing in the
    current *or* the preceding calendar year.
    """
    current_year = year if year is not None else date.today().year
    config = crud_store_config.get_singleton(db)
    shipping_vat_rate = config.invoice_vat_rate
    fx_rates = config.fx_rates or {}

    current = _year_total(
        db, year=current_year, shipping_vat_rate=shipping_vat_rate, fx_rates=fx_rates
    )
    prior = _year_total(
        db, year=current_year - 1, shipping_vat_rate=shipping_vat_rate, fx_rates=fx_rates
    )
    return EuDistanceSellingStatus(
        threshold_eur=EU_DISTANCE_SELLING_THRESHOLD_EUR, current=current, prior=prior
    )
