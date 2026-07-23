"""Gift card models — admin-issued stored-value instruments (bearer money).

A gift card is denominated in exactly one currency (D3 — no runtime conversion,
consistent with the per-currency price columns) and is spendable as a *tender*
against an order total (D2 — it is not a discount; checkout wiring is BE PR2).
v1 cards are **admin-issued only** (D1): there is no purchasable gift-card
product, hence no voucher-sale VAT event.

Codes are bearer money, so they are **hashed at rest** (D4): the table stores
``code_hash`` (sha256 hex of the normalized plaintext, unique-indexed) plus a
``last4`` display fragment. The plaintext exists only in the 201 issue response
and the issue email — there is deliberately no way to read it back (re-issue
instead of resend).

``status`` follows the ``CouponDiscountType`` convention: a ``str``-enum stored
as the lowercase value in a plain ``String(16)`` column, guarded by a CHECK
constraint. Every balance mutation writes a ``GiftCardEntry`` ledger row
(signed amount + ``balance_after``) in the same unit of work, under
``SELECT … FOR UPDATE`` on the card row (D5) — see ``crud/gift_card.py``.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    pass


class GiftCardStatus(str, enum.Enum):
    """Lifecycle of a gift card (admin-controlled; expiry is a separate column).

    ``ACTIVE`` cards are redeemable (subject to expiry/balance/currency checks);
    ``DISABLED`` cards are refused with the ``gift_card_disabled`` machine code.
    A depleted card stays ``ACTIVE`` — "no balance left" is derived from
    ``balance``, not a status flip, so an admin adjustment can top it back up.
    """

    ACTIVE = "active"
    DISABLED = "disabled"


# Mirror of ``GiftCardStatus`` values for the DB CHECK constraint + the schema
# Literal, so route layer, DB layer, and OpenAPI can never drift (mirrors
# ``COUPON_DISCOUNT_TYPE_VALUES``).
GIFT_CARD_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in GiftCardStatus)


class GiftCardEntryKind(str, enum.Enum):
    """Why a gift card's balance moved (the ledger ``kind``).

    ``ISSUE`` seeds the opening balance; ``REDEEM`` debits at order creation
    (PR2); ``RELEASE`` credits back on order cancel (PR2, symmetric to stock
    restore); ``REFUND`` is reserved for refund-driven credits; ``ADJUST`` is
    the admin's signed manual correction.
    """

    ISSUE = "issue"
    REDEEM = "redeem"
    RELEASE = "release"
    REFUND = "refund"
    ADJUST = "adjust"


GIFT_CARD_ENTRY_KIND_VALUES: tuple[str, ...] = tuple(k.value for k in GiftCardEntryKind)


class GiftCard(BaseModel):
    """One stored-value card: hashed code + single-currency balance.

    ``initial_value`` snapshots what the card was issued for; ``balance`` is the
    live spendable amount (``0 <= balance``, DB-guarded). ``recipient_email`` /
    ``user_id`` are optional metadata — a gift card is a *bearer* instrument
    (D8: guests may redeem), so neither gates redemption. ``user_id`` and
    ``issued_by_user_id`` are SET NULL so deleting a user never breaks the
    financial record.
    """

    __tablename__ = "gift_cards"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "', '".join(GIFT_CARD_STATUS_VALUES) + "')",
            name="ck_gift_cards_status_enum",
        ),
        CheckConstraint("balance >= 0", name="ck_gift_cards_balance_non_negative"),
        CheckConstraint("initial_value > 0", name="ck_gift_cards_initial_value_positive"),
    )

    # sha256 hex of the normalized plaintext code (uppercased, separators
    # stripped) — the only lookup key; the plaintext is never stored.
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Last 4 characters of the plaintext, for admin display / search only.
    last4: Mapped[str] = mapped_column(String(4))
    initial_value: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16), default=GiftCardStatus.ACTIVE.value)
    # Optional admin-set expiry (naive UTC, like every datetime column — D9).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    recipient_email: Mapped[str | None] = mapped_column(String(255), index=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    note: Mapped[str | None] = mapped_column(Text)
    issued_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )
    # Provenance for a card issued by a *purchase* (v2): the order whose payment
    # minted it. NULL for admin-issued cards (v1). SET NULL keeps the card if
    # the order row is ever removed — the card is bearer money, independent of
    # its origin.
    order_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )

    # Ledger, oldest-first by default; routes re-order newest-first for display.
    entries: Mapped[list[GiftCardEntry]] = relationship(
        "GiftCardEntry",
        back_populates="gift_card",
        cascade="all, delete-orphan",
        order_by="GiftCardEntry.id",
    )

    def __repr__(self) -> str:
        return f"<GiftCard …{self.last4} {self.balance} {self.currency} ({self.status})>"


class GiftCardEntry(BaseModel):
    """One signed balance movement on a gift card (append-only ledger).

    ``amount`` is signed (positive = credit, negative = debit) and
    ``balance_after`` snapshots the card balance the movement produced — written
    in the same unit of work as the balance update, under the card-row lock, so
    the ledger always reconciles. ``order_id`` links redeem/release rows to the
    order that moved the money (SET NULL keeps the financial record if an order
    row ever goes away); ``actor_user_id`` records the admin behind a manual
    ``adjust``.
    """

    __tablename__ = "gift_card_entries"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('" + "', '".join(GIFT_CARD_ENTRY_KIND_VALUES) + "')",
            name="ck_gift_card_entries_kind_enum",
        ),
        CheckConstraint("balance_after >= 0", name="ck_gift_card_entries_balance_after_non_neg"),
    )

    gift_card_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("gift_cards.id", ondelete="CASCADE"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    kind: Mapped[str] = mapped_column(String(16))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    order_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)

    gift_card: Mapped[GiftCard] = relationship("GiftCard", back_populates="entries")

    def __repr__(self) -> str:
        return f"<GiftCardEntry card={self.gift_card_id} {self.kind} {self.amount}>"
