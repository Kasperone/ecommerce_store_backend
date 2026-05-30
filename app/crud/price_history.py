"""CRUDPriceHistory — the single chokepoint for the OMNIBUS price ledger.

``record_if_changed`` is the only writer; it skips when the price matches the
most recent (product, currency) row, so calling it on every product POST/PUT
is idempotent — no spam rows when an admin clicks Save without touching a
price.

``lowest_in_last_30_days`` is the read used by the public endpoint + the admin
hint. Caller owns the commit (same contract as ``crud_inventory.apply_delta``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models.price_history import PriceHistory

if TYPE_CHECKING:
    from app.models.product import Product

WINDOW_DAYS = 30


def _utcnow_naive() -> datetime:
    """Naive UTC — matches the project's existing DateTime column convention."""
    return datetime.now(UTC).replace(tzinfo=None)


class CRUDPriceHistory:
    """Append-only price ledger ops (no commit; caller owns the txn)."""

    def get_latest(self, db: Session, *, product_id: int, currency: str) -> PriceHistory | None:
        """Most recent row for a (product, currency) pair, or None.

        Used by ``record_if_changed`` for the idempotency check and exposed as
        a small public read for the admin hint "since when".
        """
        return (
            db.query(PriceHistory)
            .filter(
                PriceHistory.product_id == product_id,
                PriceHistory.currency == currency,
            )
            .order_by(PriceHistory.effective_from.desc(), PriceHistory.id.desc())
            .first()
        )

    def record_if_changed(
        self,
        db: Session,
        *,
        product: Product,
        currency: str,
        new_price: float | None,
    ) -> PriceHistory | None:
        """Append a history row iff ``new_price`` differs from the most recent.

        Returns the new row (or the unchanged existing one) for the caller to
        inspect, or ``None`` when ``new_price`` is None (the storefront treats
        "cleared price" as "not for sale in this currency" — not a comparison-
        worthy event).

        Idempotent: re-calling with the same price is a no-op. The chokepoint
        flushes so the freshly-inserted row is visible to the immediate next
        ``get_latest`` in the same transaction (the test suite + future loops
        rely on this).

        Caller commits.
        """
        if new_price is None:
            return None
        if currency not in {"USD", "PLN", "EUR"}:
            raise ValueError(f"Unsupported currency: {currency!r}")
        rounded = round(float(new_price), 2)
        latest = self.get_latest(db, product_id=product.id, currency=currency)
        if latest is not None and round(latest.price, 2) == rounded:
            return latest

        row = PriceHistory(
            product_id=product.id,
            currency=currency,
            price=rounded,
            effective_from=_utcnow_naive(),
        )
        db.add(row)
        db.flush()
        return row

    def lowest_in_last_30_days(
        self, db: Session, *, product_id: int, currency: str
    ) -> tuple[float, datetime] | None:
        """Return ``(lowest_price, since)`` for the 30-day window, or None.

        Falls back to the most-recent row when no rows fall within the window —
        a product whose price hasn't moved in 30+ days has, by definition, that
        most-recent price as both its current AND its 30-day-low. Returning
        ``None`` is reserved for the "no history at all" case (which only
        happens for products created without going through the chokepoint —
        the backfill migration covers all pre-existing ones).
        """
        cutoff = _utcnow_naive() - timedelta(days=WINDOW_DAYS)
        in_window = (
            db.query(PriceHistory)
            .filter(
                PriceHistory.product_id == product_id,
                PriceHistory.currency == currency,
                PriceHistory.effective_from >= cutoff,
            )
            .order_by(PriceHistory.price.asc(), PriceHistory.effective_from.asc())
            .first()
        )
        if in_window is not None:
            return (in_window.price, in_window.effective_from)

        latest = self.get_latest(db, product_id=product_id, currency=currency)
        if latest is None:
            return None
        return (latest.price, latest.effective_from)


price_history = CRUDPriceHistory()
