"""Coupon CRUD — admin-managed discount codes + checkout redemption.

The admin-management half mirrors ``CRUDWarehouse``: no commit anywhere (the route
owns the transaction so the mutation and its audit row land together). ``code`` is
matched case-insensitively via an uppercased comparison — the schema already
uppercases on the way in, so the stored value is canonical, and ``get_by_code``
uppercases the lookup argument defensively.

The redemption half (added for coupon-at-checkout) lives here too so the discount
math and the eligibility rules sit next to the model they read:

- ``compute_discount`` — pure: percent → ``subtotal * value / 100``; fixed →
  ``min(value, subtotal)`` (a coupon never discounts below zero); free_shipping
  → 0 (its saving lives in the shipping line, not the goods). Rounded to 2dp.
- ``effective_saving`` — what a coupon is *worth* on a given cart: the goods
  discount for percent/fixed, the shipping amount for free_shipping. This is
  the number ``pick_best`` compares candidates by.
- ``validate_for_redemption`` — runs every eligibility check (active / window /
  usage cap / currency match / min-order) against a given subtotal + currency and
  returns ``(coupon, discount)`` or raises ``CouponRedemptionError`` carrying a
  machine ``code`` + human message.
- ``pick_best`` — the single source of truth for which discount an order gets:
  validates an optionally typed code (re-raising its failure — a bad code never
  silently falls back to an automatic), evaluates every active automatic coupon
  (silently skipping ineligible ones), and returns the single best discount
  (largest effective saving; tie → manual wins, then lowest id). Used by both
  the preview endpoint and the order-create path, so the preview can never
  diverge from what checkout actually applies.
- ``redeem`` — atomically bumps ``used_count`` with a guarded UPDATE so two
  concurrent redemptions can't exceed ``max_uses`` (the validate-then-increment
  window is otherwise racy). Caller owns the commit.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.money import ZERO_MONEY, quantize_money
from app.models.coupon import Coupon, CouponDiscountType, CouponRedemption
from app.schemas.coupon import CouponCreate, CouponUpdate


class CouponRedemptionError(Exception):
    """A coupon failed an eligibility check at validate/redeem time.

    ``code`` is a stable machine-readable reason (``not_found``, ``inactive``,
    ``not_started``, ``expired``, ``usage_exhausted``, ``per_user_exhausted``,
    ``currency_mismatch``, ``min_order_not_met``); ``message`` is the human
    string surfaced to the
    customer. The route maps every variant to the same HTTP status — the
    distinction is for clients/tests, not for status codes.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def compute_discount(coupon: Coupon, subtotal: Decimal | float) -> Decimal:
    """Discount amount a coupon yields against ``subtotal`` (goods only).

    Percent coupons take ``value`` percent of the subtotal; fixed coupons take
    ``value`` units of their currency but never more than the subtotal (a coupon
    can zero an order but never make the total negative). free_shipping coupons
    never discount the goods — their saving is the shipping line (see
    ``effective_saving``), so this returns 0. Quantized to 2 decimals.

    ``subtotal`` may arrive as a wire ``float`` (preview endpoint) or a money
    ``Decimal`` (checkout); it's quantized to a ``Decimal`` here so the math is
    always exact base-10.
    """
    if coupon.discount_type == CouponDiscountType.FREE_SHIPPING.value:
        return ZERO_MONEY
    subtotal = quantize_money(subtotal)
    if coupon.discount_type == "percent":
        return quantize_money(subtotal * coupon.value / 100)
    return quantize_money(min(coupon.value, subtotal))


def effective_saving(
    coupon: Coupon, subtotal: Decimal | float, shipping: Decimal | float
) -> Decimal:
    """What a coupon is worth on a cart with this ``subtotal`` + ``shipping``.

    percent/fixed → the goods discount (``compute_discount``); free_shipping →
    the shipping amount the order would otherwise pay (post free-above-threshold
    — when that already zeroed shipping, a free_shipping coupon saves nothing
    and a goods-discount candidate wins the pick instead).
    """
    if coupon.discount_type == CouponDiscountType.FREE_SHIPPING.value:
        return quantize_money(shipping)
    return compute_discount(coupon, subtotal)


@dataclass(frozen=True)
class AppliedDiscount:
    """The picker's winner: which coupon applies and what it's worth.

    ``saving`` is the effective saving (goods discount for percent/fixed,
    shipping saved for free_shipping). ``free_shipping`` tells the consumer to
    zero the shipping line instead of discounting the goods. ``is_manual``
    distinguishes a customer-typed code from an automatic — checkout re-raises a
    redeem failure for a manual winner (the customer must know their code didn't
    work) but retries the pick without an exhausted automatic.
    """

    coupon: Coupon
    saving: Decimal
    free_shipping: bool
    is_manual: bool


class CRUDCoupon:
    """Coupon rows (no commit; caller owns the txn)."""

    def get(self, db: Session, id: int) -> Coupon | None:
        return db.get(Coupon, id)

    def get_by_code(self, db: Session, *, code: str) -> Coupon | None:
        """Look up by code (case-insensitive — codes are stored UPPERCASE)."""
        return db.query(Coupon).filter(Coupon.code == code.strip().upper()).first()

    def get_multi(
        self,
        db: Session,
        *,
        is_active: bool | None = None,
        search: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[Coupon]:
        query = self._filtered(db, is_active=is_active, search=search)
        return (
            query.order_by(Coupon.created_at.desc(), Coupon.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count(
        self, db: Session, *, is_active: bool | None = None, search: str | None = None
    ) -> int:
        return self._filtered(db, is_active=is_active, search=search).count()

    def _filtered(self, db: Session, *, is_active: bool | None, search: str | None):
        query = db.query(Coupon)
        if is_active is not None:
            query = query.filter(Coupon.is_active == is_active)
        if search:
            query = query.filter(Coupon.code.ilike(f"%{search.strip()}%"))
        return query

    def create(self, db: Session, *, obj_in: CouponCreate) -> Coupon:
        """Stage a new coupon (id populated via flush; caller commits)."""
        coupon = Coupon(
            code=obj_in.code,
            description=obj_in.description,
            discount_type=obj_in.discount_type,
            value=obj_in.value,
            currency=obj_in.currency,
            min_order_amount=obj_in.min_order_amount,
            max_uses=obj_in.max_uses,
            per_user_limit=obj_in.per_user_limit,
            starts_at=obj_in.starts_at,
            ends_at=obj_in.ends_at,
            is_active=obj_in.is_active,
            is_automatic=obj_in.is_automatic,
        )
        db.add(coupon)
        db.flush()
        db.refresh(coupon)
        return coupon

    def update(self, db: Session, *, coupon: Coupon, obj_in: CouponUpdate) -> Coupon:
        """Apply a partial update (no flush — the audit writer reads ``get_history``
        between this and the caller's commit). Only fields explicitly sent are
        touched; ``used_count`` is never settable here."""
        data = obj_in.model_dump(exclude_unset=True)
        for field, value in data.items():
            setattr(coupon, field, value)
        db.add(coupon)
        return coupon

    def delete(self, db: Session, *, coupon: Coupon) -> None:
        db.delete(coupon)

    def redemption_count_for_user(self, db: Session, *, coupon_id: int, user_id: int) -> int:
        """How many times ``user_id`` has already redeemed coupon ``coupon_id``."""
        return db.execute(
            select(func.count())
            .select_from(CouponRedemption)
            .where(
                CouponRedemption.coupon_id == coupon_id,
                CouponRedemption.user_id == user_id,
            )
        ).scalar_one()

    def validate_for_redemption(
        self,
        db: Session,
        *,
        code: str,
        subtotal: Decimal | float,
        currency: str,
        user_id: int | None = None,
        now: datetime | None = None,
    ) -> tuple[Coupon, Decimal]:
        """Validate a coupon for use against ``subtotal`` in ``currency``.

        Returns ``(coupon, discount)`` when every eligibility rule passes, else
        raises ``CouponRedemptionError`` with a machine ``code``. Existence is
        checked here; the remaining rules live in ``check_eligibility`` (shared
        with the automatic-coupon scan).
        """
        coupon = self.get_by_code(db, code=code)
        if coupon is None:
            raise CouponRedemptionError("not_found", "This coupon code is not valid.")
        self.check_eligibility(
            db, coupon=coupon, subtotal=subtotal, currency=currency, user_id=user_id, now=now
        )
        return coupon, compute_discount(coupon, subtotal)

    def check_eligibility(
        self,
        db: Session,
        *,
        coupon: Coupon,
        subtotal: Decimal | float,
        currency: str,
        user_id: int | None = None,
        now: datetime | None = None,
    ) -> None:
        """Raise ``CouponRedemptionError`` if ``coupon`` can't apply right now.

        Checks, in order: active → started → not expired → usage cap → per-user
        cap → currency match (fixed coupons only) → minimum order. The per-user
        cap is only checked when ``user_id`` is given (anonymous previews and
        guest orders skip it; the authed order-create path always passes it and
        re-validates server-side). The ``now`` override keeps the window checks
        testable without freezegun; it defaults to naive UTC to match the
        (naive) ``starts_at`` / ``ends_at`` columns.
        """
        if not coupon.is_active:
            raise CouponRedemptionError("inactive", "This coupon is no longer active.")

        moment = now if now is not None else datetime.now(UTC).replace(tzinfo=None)
        if coupon.starts_at is not None and moment < coupon.starts_at:
            raise CouponRedemptionError("not_started", "This coupon is not valid yet.")
        if coupon.ends_at is not None and moment >= coupon.ends_at:
            raise CouponRedemptionError("expired", "This coupon has expired.")

        if coupon.max_uses is not None and coupon.used_count >= coupon.max_uses:
            raise CouponRedemptionError(
                "usage_exhausted", "This coupon has reached its usage limit."
            )

        if (
            coupon.per_user_limit is not None
            and user_id is not None
            and self.redemption_count_for_user(db, coupon_id=coupon.id, user_id=user_id)
            >= coupon.per_user_limit
        ):
            raise CouponRedemptionError(
                "per_user_exhausted",
                "You have already used this coupon the maximum number of times.",
            )

        if coupon.discount_type == "fixed" and coupon.currency != currency:
            raise CouponRedemptionError(
                "currency_mismatch",
                f"This coupon can only be used for {coupon.currency} orders.",
            )

        if coupon.min_order_amount is not None and subtotal < coupon.min_order_amount:
            raise CouponRedemptionError(
                "min_order_not_met",
                f"This coupon requires a minimum order of {coupon.min_order_amount:.2f}"
                f" {currency}.",
            )

    def get_active_automatic(self, db: Session, *, now: datetime | None = None) -> list[Coupon]:
        """Active automatic coupons, pre-filtered by window + global usage cap.

        One indexed query (``ix_coupons_automatic_active``) — runs on every
        order create and every preview. Per-coupon checks that need the cart
        (currency / min-order) or the user (per-user cap) happen in
        ``check_eligibility`` from ``pick_best``.
        """
        moment = now if now is not None else datetime.now(UTC).replace(tzinfo=None)
        return (
            db.query(Coupon)
            .filter(
                Coupon.is_automatic.is_(True),
                Coupon.is_active.is_(True),
                (Coupon.starts_at.is_(None)) | (Coupon.starts_at <= moment),
                (Coupon.ends_at.is_(None)) | (Coupon.ends_at > moment),
                (Coupon.max_uses.is_(None)) | (Coupon.used_count < Coupon.max_uses),
            )
            .order_by(Coupon.id)
            .all()
        )

    def pick_best(
        self,
        db: Session,
        *,
        code: str | None,
        subtotal: Decimal | float,
        shipping: Decimal | float,
        currency: str,
        user_id: int | None = None,
        now: datetime | None = None,
        exclude_coupon_ids: Collection[int] = (),
    ) -> AppliedDiscount | None:
        """Pick the single best discount for a cart (typed code vs automatics).

        Stacking policy: **best-single-discount wins**. A typed ``code`` is
        validated via ``validate_for_redemption`` and its failure **re-raised**
        — an invalid typed code never silently falls back to an automatic (the
        customer must know their code didn't work). Every active automatic is
        then evaluated (per-coupon ineligibility swallowed — automatics are
        opportunistic) and candidates compete on effective saving; an automatic
        that saves nothing (e.g. free_shipping when shipping is already 0) is
        not a candidate. Tie → manual wins, then lowest id. Returns ``None``
        when nothing applies.

        ``exclude_coupon_ids`` lets checkout retry the pick without an
        automatic whose ``redeem`` lost the last-use race (usage_exhausted) —
        rather than failing the whole order over a discount the customer never
        asked for.
        """
        candidates: list[AppliedDiscount] = []
        manual: Coupon | None = None
        if code:
            manual, _ = self.validate_for_redemption(
                db, code=code, subtotal=subtotal, currency=currency, user_id=user_id, now=now
            )
            candidates.append(
                AppliedDiscount(
                    coupon=manual,
                    saving=effective_saving(manual, subtotal, shipping),
                    free_shipping=manual.discount_type == CouponDiscountType.FREE_SHIPPING.value,
                    is_manual=True,
                )
            )
        for auto in self.get_active_automatic(db, now=now):
            if auto.id in exclude_coupon_ids or (manual is not None and auto.id == manual.id):
                continue
            try:
                self.check_eligibility(
                    db, coupon=auto, subtotal=subtotal, currency=currency, user_id=user_id, now=now
                )
            except CouponRedemptionError:
                continue
            saving = effective_saving(auto, subtotal, shipping)
            if saving <= 0:
                continue
            candidates.append(
                AppliedDiscount(
                    coupon=auto,
                    saving=saving,
                    free_shipping=auto.discount_type == CouponDiscountType.FREE_SHIPPING.value,
                    is_manual=False,
                )
            )
        if not candidates:
            return None
        return max(candidates, key=lambda c: (c.saving, c.is_manual, -c.coupon.id))

    def redeem(self, db: Session, *, coupon: Coupon) -> None:
        """Atomically bump ``used_count`` (caller owns the commit).

        Uses a guarded UPDATE — ``... WHERE id = :id AND (max_uses IS NULL OR
        used_count < max_uses)`` — so two concurrent checkouts can't push the
        counter past ``max_uses`` even though both passed the cap check in
        ``validate_for_redemption``. A zero rowcount means another transaction
        took the last use first → surfaces as ``usage_exhausted`` (same reason
        code as the upfront check, so the caller maps it identically).
        """
        result = db.execute(
            update(Coupon)
            .where(
                Coupon.id == coupon.id,
                (Coupon.max_uses.is_(None)) | (Coupon.used_count < Coupon.max_uses),
            )
            .values(used_count=Coupon.used_count + 1)
        )
        if result.rowcount == 0:  # pyright: ignore[reportAttributeAccessIssue] -- DML execute returns a CursorResult with rowcount
            raise CouponRedemptionError(
                "usage_exhausted", "This coupon has reached its usage limit."
            )
        # Keep the in-session instance consistent with the DB-side increment so a
        # subsequent read of ``coupon.used_count`` in the same unit of work is correct.
        db.refresh(coupon)

    def record_redemption(
        self, db: Session, *, coupon_id: int, user_id: int, order_id: int
    ) -> CouponRedemption:
        """Record a (coupon, user, order) redemption — backs the per-user cap.

        Called after the order is flushed (so ``order_id`` exists), in the same
        unit of work as ``redeem``; a rolled-back order never records a row. The
        caller owns the commit.
        """
        row = CouponRedemption(coupon_id=coupon_id, user_id=user_id, order_id=order_id)
        db.add(row)
        db.flush()
        return row


coupon = CRUDCoupon()
