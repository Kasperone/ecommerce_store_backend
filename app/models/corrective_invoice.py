"""Faktura korygująca (Polish corrective invoice) for a refund.

PL VAT law requires a **faktura korygująca** when issuing a refund against a paid
invoice (the original ``FV/YYYY/NNNNN``). It is its own legal series, separate
from the faktura series, so a refund never consumes a faktura number. The
number format defaults to ``KOR/{year}/{seq:05d}`` (overridable via
``INVOICE_KOR_NUMBER_FORMAT``).

Mirrors ``app/models/invoice.py``: one ``CorrectiveInvoice`` per ``Refund``
(``refund_id`` UNIQUE, ON DELETE RESTRICT — the KOR is a legal record), allocated
gap-free per year via a locked ``corrective_invoice_counters`` row inside the
issuing transaction (rollback reclaims the number). ``original_invoice_id`` ties
the KOR back to its source faktura so the PDF can print the corrected reference
and the legal series stays auditable.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BaseModel

if TYPE_CHECKING:
    from app.models.invoice import Invoice
    from app.models.refund import Refund


class CorrectiveInvoice(BaseModel):
    """A legally-issued faktura korygująca for one refund.

    Row exists **iff** the refund has been issued (one is created in the same
    transaction as the refund — same gap-free contract as ``Invoice``). Number,
    year, seq, issue date are stamped once at allocation and never re-computed.
    """

    __tablename__ = "corrective_invoices"
    __table_args__ = (UniqueConstraint("year", "seq", name="uq_corrective_invoices_year_seq"),)

    refund_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("refunds.id", ondelete="RESTRICT"), unique=True, index=True
    )
    original_invoice_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("invoices.id", ondelete="RESTRICT"), index=True
    )
    kor_number: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    seq: Mapped[int] = mapped_column(Integer)
    issued_at: Mapped[datetime] = mapped_column(DateTime)

    refund: Mapped[Refund] = relationship("Refund", back_populates="corrective_invoice")
    original_invoice: Mapped[Invoice] = relationship("Invoice")

    def __repr__(self) -> str:
        return f"<CorrectiveInvoice {self.kor_number} refund#{self.refund_id}>"


class CorrectiveInvoiceCounter(Base):
    """Per-year sequence counter for gap-free faktura-korygująca numbering.

    Not a ``BaseModel`` so the atomic Postgres allocator can
    ``INSERT … ON CONFLICT (year) DO UPDATE SET next_seq = next_seq + 1 RETURNING
    next_seq`` cleanly. The row lock taken by the upsert serialises concurrent
    allocations; the increment lives in the issuing transaction, so a rollback
    un-does it — the series stays gap-free. Same shape as ``InvoiceCounter``.
    """

    __tablename__ = "corrective_invoice_counters"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    next_seq: Mapped[int] = mapped_column(Integer, nullable=False)

    def __repr__(self) -> str:
        return f"<CorrectiveInvoiceCounter year={self.year} next_seq={self.next_seq}>"
