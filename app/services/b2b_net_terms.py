"""B2B net-terms (pay-on-account) resolution + credit math (Program 2).

Sibling to ``b2b_pricing.py``: the single choke point that answers "may this
authenticated user place a pay-on-account order, and how much credit is left?"

A user may pay on account iff they have an **active** company membership
(``crud_company_user.get_active_membership``) whose role is in the company's
``net_terms_allowed_roles`` and whose company has ``net_terms_enabled`` with an
assigned, active ``PaymentTerm`` (the term decides the due date). The **credit limit**
(``company.credit_limit``, denominated in ``company.default_currency``; NULL =
unlimited) hard-blocks the option once outstanding unpaid net-terms balance + the
new order would exceed it.

**Outstanding** is tracked orthogonally to fulfilment status: a net-terms order
ships while unpaid and never enters the PAID status. Outstanding = Σ ``total``
over the company's ``net_terms`` orders that are not yet paid
(``net_terms_paid_at IS NULL``) and not cancelled, in the given currency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import crud
from app.core.money import quantize_money
from app.models.company import Company
from app.models.order import Order, OrderStatus
from app.models.payment_term import PaymentTerm, PaymentTermType
from app.services.fx import Rates, convert_money

# The payment_method value stored on a pay-on-account order.
NET_TERMS_METHOD = "net_terms"


@dataclass(frozen=True)
class NetTermsContext:
    """Resolved pay-on-account context for a caller: their company + the assigned term."""

    company: Company
    term: PaymentTerm


def compute_due_date(term: PaymentTerm, order_dt: datetime) -> datetime:
    """The printed ``termin płatności`` for ``term`` given the order's placement time.

    ``order_dt`` is the naive-UTC placement moment. Returns a naive-UTC datetime:
    - ``net_days``       → order_dt + ``net_days`` days
    - ``due_on_receipt`` → order_dt itself (pay immediately)
    - ``fixed_date``     → midnight of the term's ``due_date`` calendar deadline
    """
    if term.term_type == PaymentTermType.FIXED_DATE.value:
        # A fixed_date term always carries ``due_date`` (model CHECK + schema).
        assert term.due_date is not None  # noqa: S101 -- guaranteed by CHECK/schema
        return datetime.combine(term.due_date, time())
    if term.term_type == PaymentTermType.DUE_ON_RECEIPT.value:
        return order_dt
    # net_days: ``net_days`` is guaranteed non-null for this type.
    return order_dt + timedelta(days=term.net_days or 0)


def resolve_net_terms(db: Session, *, user_id: int | None) -> NetTermsContext | None:
    """The caller's net-terms context, or ``None`` if pay-on-account doesn't apply.

    ``None`` means "not eligible": guest, non-B2B account, a membership whose role
    is not in the company's ``net_terms_allowed_roles``, or a company without net
    terms enabled / with no assigned (active) payment term.
    """
    if user_id is None:
        return None
    membership = crud.company_user.get_active_membership(db, user_id=user_id)
    if membership is None:
        return None
    company = membership.company
    if not company.net_terms_enabled:
        return None
    if membership.role not in (company.net_terms_allowed_roles or []):
        return None
    term = company.payment_term
    if term is None or not term.is_active:
        return None
    return NetTermsContext(company=company, term=term)


def _fx_rates(db: Session) -> Rates:
    """The admin-configured FX rate map (``store_config.fx_rates``; ``{}`` if unset)."""
    return crud.store_config.get_singleton(db).fx_rates or {}


def convert_to_credit_currency(
    db: Session, *, company: Company, amount: Decimal, currency: str
) -> Decimal | None:
    """Convert ``amount``/``currency`` into ``company.default_currency`` via admin FX rates.

    ``None`` when the company has no billing currency, or an FX rate is missing
    (the currencies are then incomparable — the caller rejects the pay-on-account
    option, as before Program D). Same-currency is the identity (no rate needed).
    """
    default = company.default_currency
    if default is None:
        return None
    return convert_money(amount, currency, default, _fx_rates(db))


def outstanding_balance(db: Session, *, company: Company) -> Decimal | None:
    """Σ the company's unpaid, non-cancelled net-terms orders, in its billing currency.

    Cross-currency (Program D): each order's ``total`` is converted from its own
    currency into ``company.default_currency`` via the admin FX rates. Returns
    ``None`` when the balance can't be measured — no billing currency set, or an
    order is in a currency with no configured FX rate (the caller then rejects
    the option rather than under-count AR). Same-currency orders never need a rate.
    """
    default = company.default_currency
    if default is None:
        return None
    rates = _fx_rates(db)
    # Group by currency so a mixed-currency AR needs one convert per currency.
    rows = (
        db.query(Order.currency, func.coalesce(func.sum(Order.total), 0))
        .filter(
            Order.company_id == company.id,
            Order.payment_method == NET_TERMS_METHOD,
            Order.net_terms_paid_at.is_(None),
            Order.status != OrderStatus.CANCELLED,
        )
        .group_by(Order.currency)
        .all()
    )
    total = Decimal(0)
    for ccy, subtotal in rows:
        converted = convert_money(quantize_money(Decimal(subtotal or 0)), ccy, default, rates)
        if converted is None:
            return None  # a currency lacks an FX rate — AR can't be measured
        total += converted
    return quantize_money(total)


def credit_available(db: Session, *, company: Company) -> Decimal | None:
    """Remaining credit for ``company`` in its ``default_currency``.

    ``None`` when the company has no ``credit_limit`` (unlimited) — the caller
    then treats every pay-on-account order as within budget. Never negative
    (clamped at 0). An unmeasurable balance (missing FX rate) reads as 0 available
    (conservative) for the display path; the checkout re-checks and rejects.
    """
    if company.credit_limit is None:
        return None
    outstanding = outstanding_balance(db, company=company)
    if outstanding is None:
        return Decimal("0.00")
    remaining = quantize_money(company.credit_limit - outstanding)
    return remaining if remaining > 0 else Decimal("0.00")


def build_credit_summary(db: Session, *, company: Company) -> dict[str, object]:
    """The net-terms credit summary for ``company`` (drives ``CompanyCreditResponse``).

    Shared by the admin (``GET /admin/companies/{id}/credit``) and buyer
    (``GET /companies/me/credit``) routes so the two can't diverge. Outstanding is
    reported in the company's billing currency (cross-currency AR converted via
    admin FX rates). Money is surfaced as wire ``float``; ``credit_limit`` /
    ``available`` are ``None`` for an unlimited company.
    """
    outstanding = outstanding_balance(db, company=company)
    available = credit_available(db, company=company)
    return {
        "net_terms_enabled": company.net_terms_enabled,
        "payment_term": company.payment_term,
        "currency": company.default_currency,
        "credit_limit": float(company.credit_limit) if company.credit_limit is not None else None,
        "outstanding": float(outstanding) if outstanding is not None else 0.0,
        "available": float(available) if available is not None else None,
    }
