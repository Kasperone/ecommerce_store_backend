"""CRUD for the abandoned-cart snapshot table (`saved_carts`).

Small and focused (mirrors ``app/crud/user_consent.py``): the sync upsert the
authed frontend calls, the token lookup for the public recovery endpoint, the
abandonment-candidates query for the detection job, and the shared
"has the owner ordered since?" check. No commit anywhere — the route/service
owns the transaction.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import exists, func
from sqlalchemy.orm import Session, joinedload

from app.models.order import Order
from app.models.saved_cart import SavedCart
from app.models.user import User


class CRUDSavedCart:
    """Snapshot upsert + recovery/detection reads (no commit; caller owns txn)."""

    def get_by_user(self, db: Session, *, user_id: int) -> SavedCart | None:
        """The user's single snapshot row (unique ``user_id``), or None."""
        return db.query(SavedCart).filter(SavedCart.user_id == user_id).first()

    def upsert_for_user(
        self, db: Session, *, user_id: int, items: list[dict[str, Any]]
    ) -> SavedCart | None:
        """Upsert the caller's snapshot; empty ``items`` deletes the row.

        Deep-equal items leave the row completely untouched (``updated_at`` must
        keep measuring real inactivity, not sync chatter). Any actual change
        resets the ``recovery_token``/``recovery_email_sent_at`` pair — a changed
        cart is a new abandonment candidate (decision D7). Caller commits.
        """
        row = self.get_by_user(db, user_id=user_id)
        if not items:
            if row is not None:
                db.delete(row)
            return None
        if row is None:
            row = SavedCart(user_id=user_id, items=items)
            db.add(row)
            return row
        if row.items == items:
            return row
        row.items = items
        row.recovery_token = None
        row.recovery_email_sent_at = None
        db.add(row)
        return row

    def get_by_token(self, db: Session, *, token: str) -> SavedCart | None:
        """Resolve a recovery token (opaque 256-bit credential), or None."""
        return db.query(SavedCart).filter(SavedCart.recovery_token == token).first()

    def has_newer_order(self, db: Session, *, user_id: int, since: datetime) -> bool:
        """True when the user placed any order after ``since`` (naive UTC).

        Shared by the recovery endpoint (410 ``cart_already_ordered``) and the
        detection job's candidates query — the "ordered on another device"
        escape hatch.
        """
        return db.query(
            db.query(Order).filter(Order.user_id == user_id, Order.created_at > since).exists()
        ).scalar()

    def abandoned_candidates(self, db: Session, *, cutoff: datetime) -> list[SavedCart]:
        """Snapshot rows eligible for the one-shot recovery email.

        Non-empty items, last touched before ``cutoff``, no email sent yet, owner
        still active and not anonymized, and no order created after the cart's
        ``updated_at``. Consent filtering happens in the service layer
        (``current_map`` is per-user Python logic over the append-only log).
        """
        newer_order = exists().where(
            Order.user_id == SavedCart.user_id,
            Order.created_at > SavedCart.updated_at,
        )
        return (
            db.query(SavedCart)
            .join(User, User.id == SavedCart.user_id)
            .options(joinedload(SavedCart.user))
            .filter(
                func.jsonb_array_length(SavedCart.items) > 0,
                SavedCart.updated_at < cutoff,
                SavedCart.recovery_email_sent_at.is_(None),
                User.is_active.is_(True),
                User.anonymized_at.is_(None),
                ~newer_order,
            )
            .order_by(SavedCart.updated_at.asc(), SavedCart.id.asc())
            .all()
        )

    def delete_for_user(self, db: Session, *, user_id: int) -> None:
        """Drop the user's snapshot row if present (GDPR anonymize). Caller commits."""
        db.query(SavedCart).filter(SavedCart.user_id == user_id).delete()


saved_cart = CRUDSavedCart()
