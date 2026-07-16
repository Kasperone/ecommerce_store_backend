"""B2B net-terms (pay-on-account) resolution + credit math (Program 2).

Sibling to ``b2b_pricing.py``: the single choke point that answers "may this
authenticated user place a pay-on-account order, and how much credit is left?"

A user may pay on account iff they have an **active** company membership
(``crud_company_user.get_active_membership``) whose company has
``net_terms_enabled`` and a configured ``net_terms_days``. The **credit limit**
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
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import crud
from app.core.money import quantize_money
from app.models.company import Company
from app.models.order import Order, OrderStatus

# The payment_method value stored on a pay-on-account order.
NET_TERMS_METHOD = "net_terms"


@dataclass(frozen=True)
class NetTermsContext:
    """Resolved pay-on-account context for a caller: their company + term length."""

    company: Company
    days: int


def resolve_net_terms(db: Session, *, user_id: int | None) -> NetTermsContext | None:
    """The caller's net-terms context, or ``None`` if pay-on-account doesn't apply.

    ``None`` means "not eligible" (guest, non-B2B account, or a company without
    net terms enabled / with no ``net_terms_days`` configured).
    """
    if user_id is None:
        return None
    membership = crud.company_user.get_active_membership(db, user_id=user_id)
    if membership is None:
        return None
    company = membership.company
    if not company.net_terms_enabled or company.net_terms_days is None:
        return None
    return NetTermsContext(company=company, days=company.net_terms_days)


def outstanding_balance(db: Session, *, company_id: int, currency: str) -> Decimal:
    """Σ ``total`` of the company's unpaid, non-cancelled net-terms orders in ``currency``.

    This is the accounts-receivable figure the credit limit is checked against.
    """
    total = (
        db.query(func.coalesce(func.sum(Order.total), 0))
        .filter(
            Order.company_id == company_id,
            Order.payment_method == NET_TERMS_METHOD,
            Order.net_terms_paid_at.is_(None),
            Order.currency == currency,
            Order.status != OrderStatus.CANCELLED,
        )
        .scalar()
    )
    return quantize_money(Decimal(total or 0))


def credit_available(db: Session, *, company: Company) -> Decimal | None:
    """Remaining credit for ``company`` in its ``default_currency``.

    ``None`` when the company has no ``credit_limit`` (unlimited) — the caller
    then treats every pay-on-account order as within budget. Never negative
    (clamped at 0 so an over-limit company reads "0 available", not a negative).
    """
    if company.credit_limit is None:
        return None
    currency = company.default_currency
    outstanding = (
        outstanding_balance(db, company_id=company.id, currency=currency)
        if currency is not None
        else Decimal(0)
    )
    remaining = quantize_money(company.credit_limit - outstanding)
    return remaining if remaining > 0 else Decimal("0.00")


def build_credit_summary(db: Session, *, company: Company) -> dict[str, object]:
    """The net-terms credit summary for ``company`` (drives ``CompanyCreditResponse``).

    Shared by the admin (``GET /admin/companies/{id}/credit``) and buyer
    (``GET /companies/me/credit``) routes so the two can't diverge. Money is
    surfaced as wire ``float`` (the schema's field type); ``credit_limit`` /
    ``available`` are ``None`` for an unlimited company.
    """
    currency = company.default_currency
    outstanding = (
        outstanding_balance(db, company_id=company.id, currency=currency)
        if currency is not None
        else Decimal("0.00")
    )
    available = credit_available(db, company=company)
    return {
        "net_terms_enabled": company.net_terms_enabled,
        "net_terms_days": company.net_terms_days,
        "currency": currency,
        "credit_limit": float(company.credit_limit) if company.credit_limit is not None else None,
        "outstanding": float(outstanding),
        "available": float(available) if available is not None else None,
    }
