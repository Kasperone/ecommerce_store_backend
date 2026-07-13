"""Server-side cart snapshot for abandoned-cart recovery (`saved_carts`).

The cart itself lives entirely in the browser (Pinia + localStorage); this
table is a minimal ids+quantities mirror the frontend upserts for
**authenticated** users so abandonment can be detected at all. One row per
user (unique ``user_id``); an emptied cart (checkout or manual clear) deletes
the row rather than keeping an empty snapshot.

``updated_at`` (from ``TimestampMixin``) is the abandonment clock: the
detection job (``app.scripts.send_abandoned_cart_emails``) picks rows whose
last change is older than the configured threshold. ``recovery_token`` is an
opaque 256-bit ``secrets.token_urlsafe(32)`` credential stored plaintext with
a unique index — the exact ``Order.guest_token`` precedent — and it restores
cart **contents only**, never authenticates. ``recovery_email_sent_at``
doubles as the "one email per abandoned cart" latch and the TTL anchor; both
are reset to NULL whenever the items change (a changed cart is a new
abandonment candidate).
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.user import User


class SavedCart(BaseModel):
    """One authed user's current cart snapshot (ids + quantities only).

    ``items`` is a JSONB list of ``{"product_id": int, "variant_id": int | None,
    "quantity": int}`` — no price/name snapshots, the recovery endpoint and the
    email both read live catalog data. Cascades on (hard) user delete; the GDPR
    anonymize flow deletes the row explicitly (the user row itself is kept).
    """

    __tablename__ = "saved_carts"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    # [{"product_id": int, "variant_id": int|None, "quantity": int}, ...]
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    recovery_token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    recovery_email_sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    user: Mapped["User"] = relationship("User", passive_deletes=True)

    def __repr__(self) -> str:
        return f"<SavedCart user={self.user_id} lines={len(self.items or [])}>"
