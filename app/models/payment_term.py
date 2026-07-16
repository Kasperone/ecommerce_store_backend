"""Payment-term template model — named B2B net-terms terms (net-terms tail Program A).

A ``PaymentTerm`` is admin-managed management metadata (same posture as
``PriceList`` / ``Coupon``): a **named, reusable** payment term assigned to a
company (``companies.payment_term_id``). It replaces the bare ``net_terms_days``
int that Program 2 stamped on the company directly — a company now points at a
term template, and the term decides the printed ``termin płatności`` (due date):

- ``net_days``        → due = order date + ``net_days``       (classic "Net 30")
- ``due_on_receipt``  → due = order date                       (pay immediately)
- ``fixed_date``      → due = the template's ``due_date``       (a calendar deadline)

``term_type`` follows the ``CompanyStatus`` convention: a ``str``-enum stored as
the lowercase value in a plain ``String(20)`` column (not a PG enum), guarded by
a CHECK constraint — adding a value later is a code-only change. ``net_days`` is
required for the ``net_days`` type; ``due_date`` for ``fixed_date``; neither for
``due_on_receipt`` (validated at the schema layer, mirrored by the CHECK here).
"""

from __future__ import annotations

import enum
from datetime import date

from sqlalchemy import Boolean, CheckConstraint, Date, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class PaymentTermType(str, enum.Enum):
    """How a payment term derives its due date.

    Stored as the lowercase value in a ``String(20)`` column (mirrors
    ``CompanyStatus``). ``NET_DAYS`` = order date + ``net_days``;
    ``DUE_ON_RECEIPT`` = the order date itself; ``FIXED_DATE`` = the template's
    explicit ``due_date`` calendar deadline.
    """

    NET_DAYS = "net_days"
    DUE_ON_RECEIPT = "due_on_receipt"
    FIXED_DATE = "fixed_date"


PAYMENT_TERM_TYPE_VALUES: tuple[str, ...] = tuple(t.value for t in PaymentTermType)


class PaymentTerm(BaseModel):
    """An admin-managed, named payment term assigned to companies.

    ``name`` is required (e.g. "Net 30", "Due on receipt", "Due 2026-12-31");
    ``is_active`` gates whether the term is offered at checkout (an inactive
    term makes pay-on-account unavailable for any company pointing at it, same
    as an unset term). ``payment_terms.id`` is the stable contract
    ``companies.payment_term_id`` FKs against (ON DELETE SET NULL).
    """

    __tablename__ = "payment_terms"
    __table_args__ = (
        CheckConstraint(
            "term_type IN ('net_days', 'due_on_receipt', 'fixed_date')",
            name="ck_payment_terms_type_enum",
        ),
        # net_days terms must carry a day count; fixed_date terms must carry a
        # date; due_on_receipt needs neither. Enforced here + at the schema layer.
        CheckConstraint(
            "(term_type = 'net_days' AND net_days IS NOT NULL) "
            "OR (term_type = 'fixed_date' AND due_date IS NOT NULL) "
            "OR (term_type = 'due_on_receipt')",
            name="ck_payment_terms_shape",
        ),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    term_type: Mapped[str] = mapped_column(String(20), nullable=False)
    net_days: Mapped[int | None] = mapped_column(Integer)
    due_date: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text)
