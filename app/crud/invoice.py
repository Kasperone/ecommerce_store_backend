"""Invoice CRUD: the single choke-point that issues sequential faktura numbers.

``issue_for_order`` is idempotent — it returns the existing registry row or
allocates the next gap-free sequence for the current year and inserts one. It
does **not** commit (the caller owns the transaction, same contract as
``crud_inventory.apply_delta``), so the number and the order's ``pending → paid``
flip land or roll back together.

Gap-free numbering comes from the per-year ``invoice_counters`` row: a Postgres
``INSERT … ON CONFLICT DO UPDATE … RETURNING`` is atomic and row-locked, and the
increment lives in the issuing transaction, so a rollback reclaims the number.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.invoice import Invoice

if TYPE_CHECKING:
    from app.models.order import Order


class CRUDInvoice:
    """Faktura-numbering choke-point + registry reads (no commit; caller owns the txn)."""

    def get_by_order_id(self, db: Session, order_id: int) -> Invoice | None:
        """Return the issued invoice for an order, or None if none has been issued."""
        return db.query(Invoice).filter(Invoice.order_id == order_id).first()

    def _allocate_seq(self, db: Session, year: int) -> int:
        """Atomically take the next sequence number for ``year`` (gap-free).

        The upsert locks the counter row for the duration of the transaction, so
        concurrent issuers serialise; the returned value is the sequence to use.
        Because the increment is in the caller's transaction, a rollback un-does it.
        """
        return int(
            db.execute(
                text(
                    "INSERT INTO invoice_counters (year, next_seq) VALUES (:year, 1) "
                    "ON CONFLICT (year) DO UPDATE SET next_seq = invoice_counters.next_seq + 1 "
                    "RETURNING next_seq"
                ),
                {"year": year},
            ).scalar_one()
        )

    def _render_number(self, *, year: int, month: int, seq: int) -> str:
        """Render the configured ``INVOICE_NUMBER_FORMAT`` template (validated at startup)."""
        return settings.INVOICE_NUMBER_FORMAT.format(
            prefix=settings.INVOICE_NUMBER_PREFIX, year=year, month=month, seq=seq
        )

    def issue_for_order(self, db: Session, *, order: Order) -> Invoice:
        """Issue (or return the already-issued) faktura for an order. Idempotent.

        Allocates the next gap-free sequence for the issue year, renders the number,
        and inserts the registry row. ``db.flush()`` surfaces a unique violation now
        and populates the id; the caller owns the commit.
        """
        existing = self.get_by_order_id(db, order.id)
        if existing is not None:
            return existing

        issued_at = datetime.now(UTC).replace(tzinfo=None)  # naive UTC, project convention
        seq = self._allocate_seq(db, issued_at.year)
        invoice = Invoice(
            order_id=order.id,
            invoice_number=self._render_number(year=issued_at.year, month=issued_at.month, seq=seq),
            year=issued_at.year,
            seq=seq,
            issued_at=issued_at,
        )
        db.add(invoice)
        db.flush()
        return invoice


invoice = CRUDInvoice()
