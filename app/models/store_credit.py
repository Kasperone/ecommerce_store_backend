"""Store-credit models — per-customer, per-currency stored-value accounts.

Store credit is inherently account-bound (D8: no bearer semantics — only the
authenticated owner can spend it) and per-currency (D3: one account row per
``user_id x currency``, UNIQUE-guarded; no runtime conversion). It is granted
by admins (manual adjust) and — from BE PR2 — by refunds with
``destination="store_credit"`` (D6), and spent as a *tender* against the order
total (D2).

Every balance mutation writes a ``StoreCreditEntry`` ledger row (signed amount
+ ``balance_after``) in the same unit of work, under ``SELECT … FOR UPDATE`` on
the account row (D5) — see ``crud/store_credit.py``. ``kind`` follows the
str-enum-in-``String(16)``-with-CHECK convention (``CouponDiscountType`` /
``GiftCardStatus``).
"""

from __future__ import annotations

import enum
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class StoreCreditEntryKind(str, enum.Enum):
    """Why a store-credit balance moved (the ledger ``kind``).

    ``ADJUST`` is the admin's signed manual grant/correction; ``REDEEM`` debits
    at order creation (PR2); ``RELEASE`` credits back on order cancel (PR2);
    ``REFUND`` credits from a refund with ``destination="store_credit"`` (PR2).
    """

    ADJUST = "adjust"
    REDEEM = "redeem"
    RELEASE = "release"
    REFUND = "refund"


STORE_CREDIT_ENTRY_KIND_VALUES: tuple[str, ...] = tuple(k.value for k in StoreCreditEntryKind)


class StoreCreditAccount(BaseModel):
    """One customer's balance in one currency (``UNIQUE(user_id, currency)``).

    Rows are created lazily on the first credit — most customers never have
    one. ``balance >= 0`` is DB-guarded on top of the CRUD-layer check.
    ``user_id`` cascades: store credit is worthless without its owner (unlike
    gift cards, which are bearer instruments and survive user deletion).
    """

    __tablename__ = "store_credit_accounts"
    __table_args__ = (
        UniqueConstraint("user_id", "currency", name="uq_store_credit_accounts_user_currency"),
        CheckConstraint("balance >= 0", name="ck_store_credit_accounts_balance_non_negative"),
    )

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    currency: Mapped[str] = mapped_column(String(3))
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))

    entries: Mapped[list[StoreCreditEntry]] = relationship(
        "StoreCreditEntry",
        back_populates="account",
        cascade="all, delete-orphan",
        order_by="StoreCreditEntry.id",
    )

    def __repr__(self) -> str:
        return f"<StoreCreditAccount user={self.user_id} {self.balance} {self.currency}>"


class StoreCreditEntry(BaseModel):
    """One signed balance movement on a store-credit account (append-only ledger).

    Mirrors ``GiftCardEntry``: signed ``amount`` + ``balance_after`` snapshot,
    written under the account-row lock in the same unit of work as the balance
    update. ``order_id`` links redeem/release rows; ``refund_id`` links a
    refund-driven credit (D6, PR2); ``actor_user_id`` records the admin behind
    a manual ``adjust``. All three are SET NULL — the ledger is a financial
    record and outlives its references.
    """

    __tablename__ = "store_credit_entries"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('" + "', '".join(STORE_CREDIT_ENTRY_KIND_VALUES) + "')",
            name="ck_store_credit_entries_kind_enum",
        ),
        CheckConstraint("balance_after >= 0", name="ck_store_credit_entries_balance_after_non_neg"),
    )

    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("store_credit_accounts.id", ondelete="CASCADE"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    kind: Mapped[str] = mapped_column(String(16))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    order_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )
    refund_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("refunds.id", ondelete="SET NULL")
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)

    account: Mapped[StoreCreditAccount] = relationship(
        "StoreCreditAccount", back_populates="entries"
    )

    def __repr__(self) -> str:
        return f"<StoreCreditEntry account={self.account_id} {self.kind} {self.amount}>"
