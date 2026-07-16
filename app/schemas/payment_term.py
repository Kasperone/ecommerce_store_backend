"""Payment-term schemas — named B2B net-terms templates (net-terms tail Program A).

A payment term is a header only (name + type + the type's parameter + active
flag + notes). Cross-field validation enforces the shape the model's CHECK
mirrors: ``net_days`` terms need ``net_days``; ``fixed_date`` terms need
``due_date``; ``due_on_receipt`` needs neither.
"""

from __future__ import annotations

from datetime import date

from pydantic import Field, model_validator

from app.models.payment_term import PaymentTermType
from app.schemas.base import BaseSchema, TimestampSchema


class PaymentTermBase(BaseSchema):
    """Shared payment-term fields (create + response)."""

    name: str = Field(min_length=1, max_length=100)
    term_type: PaymentTermType
    net_days: int | None = Field(default=None, ge=0, le=365)
    due_date: date | None = None
    is_active: bool = True
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _check_shape(self) -> PaymentTermBase:
        if self.term_type == PaymentTermType.NET_DAYS:
            if self.net_days is None:
                raise ValueError("net_days is required for a net_days term")
            self.due_date = None
        elif self.term_type == PaymentTermType.FIXED_DATE:
            if self.due_date is None:
                raise ValueError("due_date is required for a fixed_date term")
            self.net_days = None
        else:  # DUE_ON_RECEIPT
            self.net_days = None
            self.due_date = None
        return self


class PaymentTermCreate(PaymentTermBase):
    """Create body (all shared fields; server assigns id/timestamps)."""


class PaymentTermUpdate(BaseSchema):
    """Partial update. ``term_type`` may be changed; when it is, the matching
    parameter (``net_days`` / ``due_date``) must be supplied — validated in the
    route against the merged row, not here (a PATCH need not resend everything)."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    term_type: PaymentTermType | None = None
    net_days: int | None = Field(default=None, ge=0, le=365)
    due_date: date | None = None
    is_active: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)


class PaymentTermResponse(PaymentTermBase, TimestampSchema):
    """Payment term with id + timestamps."""

    id: int


class PaymentTermListResponse(BaseSchema):
    """Paginated payment terms."""

    items: list[PaymentTermResponse]
    total: int
    page: int
    page_size: int
    pages: int
