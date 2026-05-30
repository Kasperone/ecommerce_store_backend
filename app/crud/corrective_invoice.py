"""Faktura-korygująca CRUD: the single choke-point that issues KOR numbers.

``issue_for_refund`` is idempotent — it returns the existing registry row or
allocates the next gap-free sequence for the current year and inserts one. It
does **not** commit (the caller owns the transaction, same contract as
``crud_invoice.issue_for_order``), so the KOR number and the refund row land
or roll back together.

Gap-free numbering comes from the per-year ``corrective_invoice_counters`` row:
a Postgres ``INSERT … ON CONFLICT DO UPDATE … RETURNING`` is atomic and
row-locked, and the increment lives in the issuing transaction so a rollback
reclaims the number.

The KOR is its own legal series, distinct from the faktura series — a refund
must never consume a faktura number.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.corrective_invoice import CorrectiveInvoice

if TYPE_CHECKING:
    from app.models.invoice import Invoice
    from app.models.refund import Refund


class CRUDCorrectiveInvoice:
    """Faktura-korygująca numbering choke-point + reads (no commit; caller owns the txn)."""

    def get_by_refund_id(self, db: Session, refund_id: int) -> CorrectiveInvoice | None:
        """Return the issued KOR for a refund, or None if none has been issued."""
        return db.query(CorrectiveInvoice).filter(CorrectiveInvoice.refund_id == refund_id).first()

    def _allocate_seq(self, db: Session, year: int) -> int:
        """Atomically take the next KOR sequence number for ``year`` (gap-free).

        The upsert locks the counter row for the duration of the transaction, so
        concurrent issuers serialise; the returned value is the sequence to use.
        Because the increment is in the caller's transaction, a rollback un-does it.
        Mirrors ``crud_invoice._allocate_seq``.
        """
        return int(
            db.execute(
                text(
                    "INSERT INTO corrective_invoice_counters (year, next_seq) VALUES (:year, 1) "
                    "ON CONFLICT (year) DO UPDATE SET "
                    "next_seq = corrective_invoice_counters.next_seq + 1 "
                    "RETURNING next_seq"
                ),
                {"year": year},
            ).scalar_one()
        )

    def _render_number(self, *, year: int, month: int, seq: int) -> str:
        """Render the configured ``INVOICE_KOR_NUMBER_FORMAT`` template (validated at startup)."""
        return settings.INVOICE_KOR_NUMBER_FORMAT.format(
            prefix=settings.INVOICE_KOR_NUMBER_PREFIX, year=year, month=month, seq=seq
        )

    def issue_for_refund(
        self, db: Session, *, refund: Refund, original_invoice: Invoice
    ) -> CorrectiveInvoice:
        """Issue (or return the already-issued) KOR for a refund. Idempotent.

        Allocates the next gap-free sequence for the issue year, renders the number,
        and inserts the registry row. ``db.flush()`` surfaces a unique violation now
        and populates the id; the caller owns the commit.
        """
        existing = self.get_by_refund_id(db, refund.id)
        if existing is not None:
            return existing

        issued_at = datetime.now(UTC).replace(tzinfo=None)  # naive UTC, project convention
        seq = self._allocate_seq(db, issued_at.year)
        kor = CorrectiveInvoice(
            refund_id=refund.id,
            original_invoice_id=original_invoice.id,
            kor_number=self._render_number(year=issued_at.year, month=issued_at.month, seq=seq),
            year=issued_at.year,
            seq=seq,
            issued_at=issued_at,
        )
        db.add(kor)
        db.flush()
        return kor


corrective_invoice = CRUDCorrectiveInvoice()
