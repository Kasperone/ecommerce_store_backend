from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BaseModel

if TYPE_CHECKING:
    from app.models.order import Order


class Invoice(BaseModel):
    """A legally-issued, sequentially-numbered VAT invoice (faktura) for an order.

    One row per order (``order_id`` UNIQUE) — so issuing is idempotent and a
    re-download always reproduces the same number + issue date. A row exists **iff**
    the order has been paid: it is created on the genuine ``pending → paid``
    transition (``crud_invoice.issue_for_order``), inside that same transaction, so
    the number and the paid state commit or roll back together. Orders without a row
    (pending, cancelled) render a *proforma* instead.

    ``order_id`` is ON DELETE RESTRICT: an invoice is a legal record and must not be
    destroyed by deleting its order (orders are never hard-deleted anyway — cancel is
    a status change). ``issued_at`` is the stable legal issue date stamped at
    allocation, not re-computed per download. Numbers are gap-free per year via the
    locked ``invoice_counters`` row; ``year``/``seq`` back the ``UNIQUE(year, seq)``
    series and the per-year reset.
    """

    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("year", "seq", name="uq_invoices_year_seq"),)

    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="RESTRICT"), unique=True, index=True
    )
    invoice_number: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    seq: Mapped[int] = mapped_column(Integer)
    issued_at: Mapped[datetime] = mapped_column(DateTime)

    order: Mapped["Order"] = relationship("Order")

    def __repr__(self) -> str:
        return f"<Invoice {self.invoice_number} order#{self.order_id}>"


class InvoiceCounter(Base):
    """Per-year sequence counter for gap-free invoice numbering.

    Deliberately **not** a ``BaseModel`` (no id/timestamps) so the atomic Postgres
    allocator can ``INSERT … ON CONFLICT (year) DO UPDATE SET next_seq = next_seq + 1
    RETURNING next_seq`` cleanly. ``next_seq`` holds the last-allocated sequence for
    the year; the row lock taken by the upsert serialises concurrent allocations, and
    because allocation runs in the issuing transaction, a rollback un-does the
    increment — so the series stays gap-free. See ``crud_invoice``.
    """

    __tablename__ = "invoice_counters"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_seq: Mapped[int] = mapped_column(Integer, nullable=False)

    def __repr__(self) -> str:
        return f"<InvoiceCounter year={self.year} next_seq={self.next_seq}>"
