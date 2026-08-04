"""Store-credit CRUD — per-customer per-currency accounts + ledger.

Mirrors ``crud/gift_card.py``: every balance mutation re-fetches the account
row ``with_for_update()`` (D5), applies exact ``Decimal`` math, and writes the
ledger entry with ``balance_after`` in the same unit of work. No commit
anywhere — the route (or PR2's order-create path) owns the transaction.

Accounts are created lazily on the first credit (``UNIQUE(user_id, currency)``
is the backstop against a creation race). ``debit`` / ``release_for_order``
are the seams BE PR2's checkout wiring will call, keyed by ``order_id``.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.money import ZERO_MONEY, quantize_money
from app.models.store_credit import (
    StoreCreditAccount,
    StoreCreditEntry,
    StoreCreditEntryKind,
)


class StoreCreditError(Exception):
    """A store-credit mutation failed a balance check.

    ``code`` is the stable machine-readable reason
    (``store_credit_balance_negative`` for admin adjusts,
    ``store_credit_insufficient`` for checkout debits); ``message`` is the
    human string. Routes map codes to statuses; clients match on ``code``.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class CRUDStoreCredit:
    """Store-credit accounts + ledger (no commit; caller owns the txn)."""

    # -- lookups ------------------------------------------------------------

    def get_account(self, db: Session, *, user_id: int, currency: str) -> StoreCreditAccount | None:
        return (
            db.query(StoreCreditAccount)
            .filter(
                StoreCreditAccount.user_id == user_id,
                StoreCreditAccount.currency == currency,
            )
            .first()
        )

    def accounts_for_user(self, db: Session, *, user_id: int) -> list[StoreCreditAccount]:
        """The user's accounts, ordered by currency for a stable display."""
        return (
            db.query(StoreCreditAccount)
            .filter(StoreCreditAccount.user_id == user_id)
            .order_by(StoreCreditAccount.currency)
            .all()
        )

    def entries_for_user(
        self, db: Session, *, user_id: int, limit: int = 100
    ) -> list[tuple[StoreCreditEntry, str]]:
        """The user's ledger across all accounts, newest first.

        Returns ``(entry, currency)`` pairs — the currency comes off the joined
        account so the response can label each movement without an N+1.
        """
        rows = (
            db.query(StoreCreditEntry, StoreCreditAccount.currency)
            .join(StoreCreditAccount, StoreCreditEntry.account_id == StoreCreditAccount.id)
            .filter(StoreCreditAccount.user_id == user_id)
            .order_by(StoreCreditEntry.created_at.desc(), StoreCreditEntry.id.desc())
            .limit(limit)
            .all()
        )
        return [(entry, currency) for entry, currency in rows]

    # -- balance mutations (all under FOR UPDATE) ----------------------------

    def _locked(self, db: Session, *, user_id: int, currency: str) -> StoreCreditAccount | None:
        """Lock the (user, currency) account row (``None`` when it doesn't exist)."""
        return (
            db.query(StoreCreditAccount)
            .filter(
                StoreCreditAccount.user_id == user_id,
                StoreCreditAccount.currency == currency,
            )
            .with_for_update()
            .first()
        )

    def _apply(
        self,
        db: Session,
        *,
        account: StoreCreditAccount,
        amount: Decimal,
        kind: str,
        error_code: str,
        order_id: int | None = None,
        refund_id: int | None = None,
        actor_user_id: int | None = None,
        note: str | None = None,
    ) -> StoreCreditEntry:
        """Move the (already locked) account's balance and write the ledger row."""
        new_balance = quantize_money(account.balance + amount)
        if new_balance < 0:
            raise StoreCreditError(error_code, "This would make the store-credit balance negative.")
        account.balance = new_balance
        entry = StoreCreditEntry(
            account_id=account.id,
            amount=quantize_money(amount),
            kind=kind,
            balance_after=new_balance,
            order_id=order_id,
            refund_id=refund_id,
            actor_user_id=actor_user_id,
            note=note,
        )
        db.add(account)
        db.add(entry)
        db.flush()
        return entry

    def adjust(
        self,
        db: Session,
        *,
        user_id: int,
        currency: str,
        amount: Decimal,
        actor_user_id: int | None = None,
        note: str | None = None,
    ) -> tuple[StoreCreditAccount, StoreCreditEntry]:
        """Admin's signed grant/correction on a customer's account.

        Creates the account lazily on first use; a negative adjust that would
        take the balance below zero (including against a not-yet-existing
        account) raises ``store_credit_balance_negative``. Caller commits.
        """
        account = self._locked(db, user_id=user_id, currency=currency)
        if account is None:
            # Lazily create on first use. A freshly created row is exclusively
            # ours until commit; the UNIQUE(user_id, currency) constraint fails
            # the loser of a concurrent-create race — fail-loud beats
            # double-crediting.
            account = StoreCreditAccount(user_id=user_id, currency=currency, balance=ZERO_MONEY)
            db.add(account)
            db.flush()
        entry = self._apply(
            db,
            account=account,
            amount=quantize_money(amount),
            kind=StoreCreditEntryKind.ADJUST.value,
            error_code="store_credit_balance_negative",
            actor_user_id=actor_user_id,
            note=note,
        )
        return account, entry

    def credit_refund(
        self,
        db: Session,
        *,
        user_id: int,
        currency: str,
        amount: Decimal,
        refund_id: int,
        order_id: int | None = None,
        actor_user_id: int | None = None,
    ) -> tuple[StoreCreditAccount, StoreCreditEntry]:
        """Credit a refund to the customer's account (D6 ``destination="store_credit"``).

        Ledger kind ``refund`` with the ``refund_id`` linked — that link is what
        ``Refund.destination`` derives from, so the credit and the refund row
        must land in the same unit of work (caller = ``crud_refund.create_for_order``,
        which owns neither commit — the route does). Creates the account lazily
        like ``adjust``.
        """
        if amount <= 0:
            raise ValueError("refund credit amount must be positive")
        account = self._locked(db, user_id=user_id, currency=currency)
        if account is None:
            # Same lazy-create + UNIQUE-backstop rationale as ``adjust``.
            account = StoreCreditAccount(user_id=user_id, currency=currency, balance=ZERO_MONEY)
            db.add(account)
            db.flush()
        entry = self._apply(
            db,
            account=account,
            amount=quantize_money(amount),
            kind=StoreCreditEntryKind.REFUND.value,
            error_code="store_credit_balance_negative",
            order_id=order_id,
            refund_id=refund_id,
            actor_user_id=actor_user_id,
        )
        return account, entry

    def debit(
        self,
        db: Session,
        *,
        user_id: int,
        currency: str,
        amount: Decimal,
        order_id: int,
    ) -> tuple[StoreCreditAccount, StoreCreditEntry]:
        """Debit ``amount`` for ``order_id`` (PR2's order-create seam, D5).

        The checkout applies ``min(balance, remainder)`` up front (D10), so a
        shortfall under the lock means a concurrent spend won the race —
        ``store_credit_insufficient`` fails the order loudly rather than
        under-paying. Caller owns the commit; a rolled-back order burns nothing.
        """
        if amount <= 0:
            raise ValueError("debit amount must be positive")
        account = self._locked(db, user_id=user_id, currency=currency)
        if account is None or quantize_money(amount) > account.balance:
            raise StoreCreditError(
                "store_credit_insufficient",
                "Not enough store credit to cover this amount.",
            )
        entry = self._apply(
            db,
            account=account,
            amount=-quantize_money(amount),
            kind=StoreCreditEntryKind.REDEEM.value,
            error_code="store_credit_insufficient",
            order_id=order_id,
        )
        return account, entry

    def release_for_order(
        self, db: Session, *, order_id: int, amount: Decimal | None = None
    ) -> Decimal:
        """Credit back what ``order_id`` redeemed (order-cancel / order-edit seam, D5).

        ``amount=None`` (the cancel case) releases everything still outstanding.
        Passing an ``amount`` releases at most that much — the order-edit case
        (SEC-019), where an order edited *downward* over-covers itself and only
        the excess comes back.

        Idempotent: the outstanding figure is ``sum(redeems) - sum(releases)``
        for the order and every release is clamped to it, so re-running (or
        cancelling an order that never used credit) is a no-op returning
        ``0.00``. **Returns what was actually released** — see the gift-card
        twin for why a caller rewriting a snapshot must use that, not the
        requested amount. Caller owns the commit.
        """
        rows = (
            db.query(StoreCreditEntry)
            .filter(
                StoreCreditEntry.order_id == order_id,
                StoreCreditEntry.kind.in_(
                    [StoreCreditEntryKind.REDEEM.value, StoreCreditEntryKind.RELEASE.value]
                ),
            )
            .all()
        )
        if not rows:
            return ZERO_MONEY
        redeemed = sum(
            (-e.amount for e in rows if e.kind == StoreCreditEntryKind.REDEEM.value), ZERO_MONEY
        )
        released = sum(
            (e.amount for e in rows if e.kind == StoreCreditEntryKind.RELEASE.value), ZERO_MONEY
        )
        outstanding = quantize_money(redeemed - released)
        to_release = outstanding if amount is None else min(quantize_money(amount), outstanding)
        if to_release <= 0:
            return ZERO_MONEY
        account = (
            db.query(StoreCreditAccount)
            .filter(StoreCreditAccount.id == rows[0].account_id)
            .with_for_update()
            .one()
        )
        self._apply(
            db,
            account=account,
            amount=to_release,
            kind=StoreCreditEntryKind.RELEASE.value,
            error_code="store_credit_balance_negative",
            order_id=order_id,
        )
        return to_release


store_credit = CRUDStoreCredit()
