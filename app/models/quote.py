"""Quote models — admin-managed B2B price quotes for a company.

A ``Quote`` is a header (which company, currency, validity window, free-form
notes, a lifecycle status, and server-computed ``subtotal`` / ``total``) plus a
set of ``QuoteItem`` line items. The parent/child shape mirrors
``Order``/``OrderItem`` (``app/models/order.py``); the per-resource admin CRUD
shape mirrors the coupons / companies verticals.

**MVP is manage-only.** Quote→order *conversion* and any customer-facing
request / negotiation flow are deliberately NOT modelled here — those touch the
shared order-total contract and a customer surface, and are separate, deferred
concerns. The admin CRUD only manages the rows. ``total == subtotal`` for now
(no tax / discount engine on a quote yet).

``status`` follows the ``RefundReason`` / ``CompanyStatus`` convention: a
``str``-enum stored as the lowercase value in a plain ``String(16)`` column (not
a PG enum), guarded by a CHECK constraint. Adding a value later is a code-only
change with no migration. Status transitions are unconstrained in MVP (any →
any) — the admin sets the value directly.

``subtotal`` / ``line_total`` / ``total`` are server-computed on write (see
``app/crud/quote.py``) — never trusted from the request body.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.company import Company


class QuoteStatus(str, enum.Enum):
    """A quote's lifecycle status.

    Stored as the lowercase value in a ``String(16)`` column (mirrors
    ``CompanyStatus`` / ``RefundReason``). ``DRAFT`` is the default for a freshly
    created quote; the rest are set by an admin. Transitions are unconstrained in
    MVP (any → any).
    """

    DRAFT = "draft"
    SENT = "sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"


# Mirror of ``QuoteStatus.values``, used by the DB CHECK constraint and
# referenced by ``app/schemas/quote.py`` so the route layer, DB layer, and
# OpenAPI schema can never drift apart. Mirrors ``COMPANY_STATUS_VALUES``.
QUOTE_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in QuoteStatus)


class Quote(BaseModel):
    """An admin-managed B2B price quote for a company.

    ``company_id`` is ON DELETE RESTRICT — a quote belongs to a company and the
    company can't be deleted out from under it (mirrors the
    ``refunds.order_id`` posture). ``currency`` is one of USD|PLN|EUR (enforced
    in the schema). ``valid_until`` is an optional expiry datetime (naive UTC,
    like every datetime column in this app); ``notes`` is free-form.

    ``subtotal`` and ``total`` are server-computed from the line items on every
    write (``total == subtotal`` in MVP — no tax / discount yet).
    """

    __tablename__ = "quotes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "', '".join(QUOTE_STATUS_VALUES) + "')",
            name="ck_quotes_status_enum",
        ),
        CheckConstraint("subtotal >= 0", name="ck_quotes_subtotal_non_negative"),
        CheckConstraint("total >= 0", name="ck_quotes_total_non_negative"),
    )

    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("companies.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft")
    currency: Mapped[str] = mapped_column(String(3))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)
    subtotal: Mapped[float] = mapped_column(Float, default=0.0)
    total: Mapped[float] = mapped_column(Float, default=0.0)
    # Conversion marker + double-convert guard: set to the created order's id when
    # the quote is converted into a pending order. ON DELETE SET NULL so deleting
    # the order leaves the quote intact (it just becomes convertible again). The
    # quote's ``status`` stays ``accepted`` after conversion — this FK is the marker.
    converted_order_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )

    # Relationships
    company: Mapped[Company] = relationship("Company")
    items: Mapped[list[QuoteItem]] = relationship(
        "QuoteItem",
        back_populates="quote",
        cascade="all, delete-orphan",
        order_by="QuoteItem.id",
    )

    @property
    def company_name(self) -> str | None:
        """Name of the quoted company (eager-loaded for list/detail views).

        Surfaced via Pydantic ``from_attributes`` so list rows carry the company
        name without a per-row N+1. Returns ``None`` only if the relationship is
        somehow unset (the RESTRICT FK means a quote always has a company).
        """
        return self.company.name if self.company is not None else None

    @property
    def item_count(self) -> int:
        """Number of line items (eager-loaded in scoped queries — no N+1)."""
        return len(self.items)

    def __repr__(self) -> str:
        return f"<Quote {self.id} company#{self.company_id} {self.status!r} {self.total}>"


class QuoteItem(BaseModel):
    """A single line on a quote.

    ``quote_id`` is ON DELETE CASCADE — deleting a quote takes its lines with it
    (matches the ``delete-orphan`` relationship cascade). ``product_id`` is an
    *optional* link to a catalog product, ON DELETE SET NULL so deleting a
    product leaves the historical quote line intact (it keeps its snapshotted
    ``description`` / ``unit_price``). ``line_total`` is computed on write
    (= ``quantity * unit_price``) — never trusted from the request body.
    """

    __tablename__ = "quote_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_quote_items_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_quote_items_unit_price_non_negative"),
        CheckConstraint("line_total >= 0", name="ck_quote_items_line_total_non_negative"),
    )

    quote_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("quotes.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="SET NULL"), index=True
    )
    description: Mapped[str] = mapped_column(String(500))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[float] = mapped_column(Float)
    line_total: Mapped[float] = mapped_column(Float)

    quote: Mapped[Quote] = relationship("Quote", back_populates="items")

    def __repr__(self) -> str:
        return f"<QuoteItem quote#{self.quote_id} {self.description!r} x{self.quantity}>"
