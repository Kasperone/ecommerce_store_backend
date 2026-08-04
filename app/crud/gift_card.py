"""Gift-card CRUD — issue/manage plus the balance-mutation seams for checkout.

Code security (D4): the plaintext code is 16 chars from an unambiguous alphabet
(no ``0/O/1/I``), generated with ``secrets.choice`` and displayed as
``XXXX-XXXX-XXXX-XXXX``. Only its sha256 hex (``code_hash``) + ``last4`` are
stored; lookup normalizes the presented code (uppercase, separators stripped)
and queries by hash. The pure helpers live at module level so they're
unit-testable without a session.

Balance mutations (D5): every mutation re-fetches the card row
``with_for_update()``, applies exact ``Decimal`` math (``quantize_money``),
and writes the ledger entry with ``balance_after`` in the same unit of work.
No commit anywhere — the route (or, in PR2, the order-create path) owns the
transaction so the balance change, its ledger row, and the caller's own writes
land (or roll back) together.

``debit`` / ``release_for_order`` are the seams BE PR2's checkout wiring will
call, keyed by ``order_id``; they're covered by unit tests now so the tender
PR only wires, never invents.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.money import ZERO_MONEY, quantize_money
from app.models.gift_card import (
    GiftCard,
    GiftCardEntry,
    GiftCardEntryKind,
    GiftCardStatus,
)

# D4 alphabet: A-Z minus I/O, 2-9 (no 0/1) — every char survives handwriting
# and phone calls. 32 symbols ** 16 chars ≈ 1.2e24 codes; the public check
# endpoint is additionally rate-limited against enumeration.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 16
_CODE_GROUP = 4


class GiftCardError(Exception):
    """A gift card failed a usability/balance check.

    ``code`` is the stable machine-readable reason pinned by the program plan
    (``gift_card_not_found`` / ``gift_card_disabled`` / ``gift_card_expired`` /
    ``gift_card_depleted`` / ``gift_card_currency_mismatch`` /
    ``gift_card_balance_negative``); ``message`` is the human string. Routes
    map codes to HTTP statuses; clients match on ``code``, never on prose.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def generate_code() -> str:
    """A fresh 16-char plaintext code from the unambiguous alphabet (D4)."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def format_code(code: str) -> str:
    """Display form ``XXXX-XXXX-XXXX-XXXX`` of a normalized 16-char code."""
    return "-".join(code[i : i + _CODE_GROUP] for i in range(0, len(code), _CODE_GROUP))


def normalize_code(code: str) -> str:
    """Canonical form of a user-presented code: uppercased, spaces/dashes stripped."""
    return code.upper().replace("-", "").replace(" ", "").strip()


def hash_code(code: str) -> str:
    """sha256 hex of the *normalized* code — the stored/queried lookup key."""
    return hashlib.sha256(normalize_code(code).encode("utf-8")).hexdigest()


def _utcnow_naive() -> datetime:
    """Naive UTC now — all datetime columns in this app are naive UTC."""
    return datetime.now(UTC).replace(tzinfo=None)


class CRUDGiftCard:
    """Gift-card rows + ledger (no commit; caller owns the txn)."""

    # -- lookups ------------------------------------------------------------

    def get(self, db: Session, id: int) -> GiftCard | None:
        return db.get(GiftCard, id)

    def get_with_entries(self, db: Session, id: int) -> GiftCard | None:
        """The card with its ledger eager-loaded (detail endpoint — no N+1)."""
        return db.execute(
            select(GiftCard).options(selectinload(GiftCard.entries)).where(GiftCard.id == id)
        ).scalar_one_or_none()

    def get_by_code(self, db: Session, *, code: str) -> GiftCard | None:
        """Look up by plaintext code (normalize → hash → query by hash)."""
        return db.query(GiftCard).filter(GiftCard.code_hash == hash_code(code)).first()

    def get_multi(
        self,
        db: Session,
        *,
        status: str | None = None,
        currency: str | None = None,
        q: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[GiftCard]:
        query = self._filtered(db, status=status, currency=currency, q=q)
        return (
            query.order_by(GiftCard.created_at.desc(), GiftCard.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count(
        self,
        db: Session,
        *,
        status: str | None = None,
        currency: str | None = None,
        q: str | None = None,
    ) -> int:
        return self._filtered(db, status=status, currency=currency, q=q).count()

    def _filtered(self, db: Session, *, status: str | None, currency: str | None, q: str | None):
        query = db.query(GiftCard)
        if status is not None:
            query = query.filter(GiftCard.status == status)
        if currency is not None:
            query = query.filter(GiftCard.currency == currency)
        if q:
            needle = q.strip()
            query = query.filter(
                or_(
                    GiftCard.last4 == needle.upper(),
                    GiftCard.recipient_email.ilike(f"%{needle}%"),
                )
            )
        return query

    # -- issue --------------------------------------------------------------

    def issue(
        self,
        db: Session,
        *,
        initial_value: Decimal,
        currency: str,
        expires_at: datetime | None = None,
        recipient_email: str | None = None,
        user_id: int | None = None,
        note: str | None = None,
        issued_by_user_id: int | None = None,
        order_id: int | None = None,
    ) -> tuple[GiftCard, str]:
        """Create a card and its opening ``issue`` ledger entry.

        Returns ``(card, plaintext_code)`` — the ONLY place the plaintext ever
        exists server-side (the route hands it to the 201 response + the issue
        email and forgets it). Regenerates on a hash collision (astronomically
        unlikely; the unique index is the backstop). Caller commits.

        ``order_id`` links a card minted by a *purchase* (v2) to the order whose
        payment issued it (provenance + double-issue guard); NULL for admin-issued
        cards (v1). It is stamped on both the card and its opening ledger entry.
        """
        value = quantize_money(initial_value)
        code = generate_code()
        while db.query(GiftCard.id).filter(GiftCard.code_hash == hash_code(code)).first():
            code = generate_code()  # pragma: no cover -- 1-in-1.2e24 collision
        card = GiftCard(
            code_hash=hash_code(code),
            last4=code[-4:],
            initial_value=value,
            balance=value,
            currency=currency,
            status=GiftCardStatus.ACTIVE.value,
            expires_at=expires_at,
            recipient_email=recipient_email,
            user_id=user_id,
            note=note,
            issued_by_user_id=issued_by_user_id,
            order_id=order_id,
        )
        db.add(card)
        db.flush()
        db.add(
            GiftCardEntry(
                gift_card_id=card.id,
                amount=value,
                kind=GiftCardEntryKind.ISSUE.value,
                balance_after=value,
                order_id=order_id,
                actor_user_id=issued_by_user_id,
            )
        )
        db.flush()
        db.refresh(card)
        return card, format_code(code)

    # -- management ---------------------------------------------------------

    def update(
        self,
        db: Session,
        *,
        card: GiftCard,
        status: str | None = None,
        expires_at: datetime | None = None,
        note: str | None = None,
        fields_set: set[str] | None = None,
    ) -> GiftCard:
        """Apply the PATCHable management fields (no flush — the audit writer
        reads ``get_history`` between this and the caller's commit).

        ``fields_set`` carries which keys the request explicitly sent so an
        explicit ``null`` clears ``expires_at`` / ``note`` while an absent key
        leaves them untouched (mirrors the preferences PATCH discipline).
        """
        fields = fields_set if fields_set is not None else set()
        if status is not None:
            card.status = status
        if "expires_at" in fields:
            card.expires_at = expires_at
        if "note" in fields:
            card.note = note
        db.add(card)
        return card

    # -- balance mutations (all under FOR UPDATE) ----------------------------

    def _locked(self, db: Session, gift_card_id: int) -> GiftCard:
        card = db.query(GiftCard).filter(GiftCard.id == gift_card_id).with_for_update().first()
        if card is None:
            raise GiftCardError("gift_card_not_found", "Gift card not found.")
        return card

    def _apply(
        self,
        db: Session,
        *,
        card: GiftCard,
        amount: Decimal,
        kind: str,
        order_id: int | None = None,
        actor_user_id: int | None = None,
        note: str | None = None,
    ) -> GiftCardEntry:
        """Move the (already locked) card's balance and write the ledger row."""
        new_balance = quantize_money(card.balance + amount)
        if new_balance < 0:
            raise GiftCardError(
                "gift_card_balance_negative",
                "This adjustment would make the gift card balance negative.",
            )
        card.balance = new_balance
        entry = GiftCardEntry(
            gift_card_id=card.id,
            amount=quantize_money(amount),
            kind=kind,
            balance_after=new_balance,
            order_id=order_id,
            actor_user_id=actor_user_id,
            note=note,
        )
        db.add(card)
        db.add(entry)
        db.flush()
        return entry

    def adjust(
        self,
        db: Session,
        *,
        gift_card_id: int,
        amount: Decimal,
        actor_user_id: int | None = None,
        note: str | None = None,
    ) -> tuple[GiftCard, GiftCardEntry]:
        """Admin's signed manual correction (positive tops up, negative claws
        back). Resulting balance must stay ``>= 0`` — else
        ``gift_card_balance_negative``. Caller commits."""
        card = self._locked(db, gift_card_id)
        entry = self._apply(
            db,
            card=card,
            amount=quantize_money(amount),
            kind=GiftCardEntryKind.ADJUST.value,
            actor_user_id=actor_user_id,
            note=note,
        )
        return card, entry

    def check_usable(self, *, card: GiftCard, currency: str, now: datetime | None = None) -> None:
        """Raise ``GiftCardError`` unless ``card`` can pay on a ``currency`` order.

        Checks, in plan order: status → expiry → balance → currency. Pure (no
        session) so the check endpoint and the locked ``debit`` share one rule
        set. The ``now`` override keeps expiry testable without freezegun.
        """
        if card.status != GiftCardStatus.ACTIVE.value:
            raise GiftCardError("gift_card_disabled", "This gift card has been disabled.")
        moment = now if now is not None else _utcnow_naive()
        if card.expires_at is not None and moment >= card.expires_at:
            raise GiftCardError("gift_card_expired", "This gift card has expired.")
        if card.balance <= 0:
            raise GiftCardError("gift_card_depleted", "This gift card has no balance left.")
        if card.currency != currency:
            raise GiftCardError(
                "gift_card_currency_mismatch",
                f"This gift card can only be used for {card.currency} orders.",
            )

    def debit(
        self,
        db: Session,
        *,
        gift_card_id: int,
        amount: Decimal,
        currency: str,
        order_id: int,
        now: datetime | None = None,
    ) -> tuple[GiftCard, GiftCardEntry]:
        """Debit ``amount`` for ``order_id`` (PR2's order-create seam, D5).

        Locks the card, re-runs every usability check *under the lock* (the
        pre-lock validation could have raced a concurrent spend), and refuses
        with ``gift_card_depleted`` when ``amount`` exceeds the live balance —
        the checkout computes ``min(balance, total)`` up front, so a shortfall
        here means a race, and failing loud beats silently under-paying.
        Writes the ``redeem`` ledger row. Caller owns the commit; a rolled-back
        order burns nothing.
        """
        if amount <= 0:
            raise ValueError("debit amount must be positive")
        card = self._locked(db, gift_card_id)
        self.check_usable(card=card, currency=currency, now=now)
        if quantize_money(amount) > card.balance:
            raise GiftCardError(
                "gift_card_depleted",
                "This gift card no longer has enough balance.",
            )
        entry = self._apply(
            db,
            card=card,
            amount=-quantize_money(amount),
            kind=GiftCardEntryKind.REDEEM.value,
            order_id=order_id,
        )
        return card, entry

    def release_for_order(
        self, db: Session, *, order_id: int, amount: Decimal | None = None
    ) -> Decimal:
        """Credit back what ``order_id`` redeemed (order-cancel / order-edit seam, D5).

        ``amount=None`` (the cancel case) releases everything still outstanding.
        Passing an ``amount`` releases at most that much — the order-edit case
        (SEC-019), where an order edited *downward* over-covers itself and only
        the excess comes back.

        Idempotent by construction: the outstanding figure is
        ``sum(redeems) - sum(releases)`` for the order, and every release is
        clamped to it, so re-running (or cancelling an order that never
        redeemed) is a no-op returning ``0.00``. The release goes through even
        on a disabled/expired card — neither cancelling nor editing an order may
        strand the customer's money. **Returns what was actually released**,
        which callers rewriting an order's tender snapshot must use rather than
        what they asked for: a short release means the ledger disagrees with the
        snapshot, and the snapshot must not claim money that never moved.
        Caller owns the commit.
        """
        rows = (
            db.query(GiftCardEntry)
            .filter(
                GiftCardEntry.order_id == order_id,
                GiftCardEntry.kind.in_(
                    [GiftCardEntryKind.REDEEM.value, GiftCardEntryKind.RELEASE.value]
                ),
            )
            .all()
        )
        if not rows:
            return ZERO_MONEY
        redeemed = sum(
            (-e.amount for e in rows if e.kind == GiftCardEntryKind.REDEEM.value), ZERO_MONEY
        )
        released = sum(
            (e.amount for e in rows if e.kind == GiftCardEntryKind.RELEASE.value), ZERO_MONEY
        )
        outstanding = quantize_money(redeemed - released)
        to_release = outstanding if amount is None else min(quantize_money(amount), outstanding)
        if to_release <= 0:
            return ZERO_MONEY
        card = self._locked(db, rows[0].gift_card_id)
        self._apply(
            db,
            card=card,
            amount=to_release,
            kind=GiftCardEntryKind.RELEASE.value,
            order_id=order_id,
        )
        return to_release


gift_card = CRUDGiftCard()
