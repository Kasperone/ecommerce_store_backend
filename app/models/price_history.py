"""Product price history — Omnibus (Directive 98/6 Art. 6a) compliance.

When a trader announces a price reduction, EU/PL law requires displaying the
**prior price**, defined as the lowest price applied during a period of not
less than 30 days before the reduction. This append-only ledger holds the
trail needed to answer "what was the lowest in the last 30 days?" for any
(product, currency) pair.

Writes go through the single chokepoint ``crud_price_history.record_if_changed``
(idempotent — no row when the price equals the most recent one). Reads use
``lowest_in_last_30_days`` for the public endpoint + admin hint. Caller owns
the commit (same contract as ``crud_inventory.apply_delta``).

History is never destroyed automatically. The admin audit log
(``admin_audit_events``) already covers *who* changed the price; this table
covers *what comparison to show legally*.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.product import Product

# Currencies the storefront supports. Mirrors the ``Product`` schema's
# per-currency columns. Adding a currency is two columns on ``products`` +
# one extra value here; the CHECK constraint below uses this tuple to render
# the SQL.
SUPPORTED_CURRENCIES: tuple[str, ...] = ("USD", "PLN", "EUR")


class PriceHistory(BaseModel):
    """One row per recorded price for a (product, currency) pair.

    ``product_id`` is ON DELETE CASCADE — the history is informational only
    (no legal record-keeping requirement on the history itself, just on the
    *display* at the moment of a price reduction), so it can disappear with
    the product. ``effective_from`` is naive UTC (project convention) and is
    populated by the CRUD chokepoint at write time, not back-dated.

    The composite index ``ix_price_history_lookup`` makes the dominant query
    ("most recent + lowest within the last 30 days for this product+currency")
    a single B-tree scan.
    """

    __tablename__ = "price_history"
    __table_args__ = (
        CheckConstraint(
            "currency IN ('" + "', '".join(SUPPORTED_CURRENCIES) + "')",
            name="ck_price_history_currency",
        ),
        CheckConstraint("price >= 0", name="ck_price_history_price_nonneg"),
        Index(
            "ix_price_history_lookup",
            "product_id",
            "currency",
            "effective_from",
        ),
    )

    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    currency: Mapped[str] = mapped_column(String(3))
    price: Mapped[float] = mapped_column(Float)
    effective_from: Mapped[datetime] = mapped_column(DateTime)

    product: Mapped[Product] = relationship("Product")

    def __repr__(self) -> str:
        return (
            f"<PriceHistory product#{self.product_id} {self.price} {self.currency} "
            f"@ {self.effective_from.isoformat()}>"
        )
