"""Draft-order CRUD — admin-composed orders-in-progress (header + catalog lines).

Mirrors ``CRUDQuote`` for the per-resource shape: no commit anywhere (the
route owns the transaction so the mutation and its audit row land together).

**Drafts hold NO stock and create NO orders** — there is no availability
check here (conversion, PR 3, validates stock). What IS validated on every
item write is the catalog reference itself: the product must exist and be
active, and the variant grain follows the shared order-create rules
(``resolve_line_variant``) — a product with active variants requires a
``variant_id``, and the variant must be that product's and active. Failures
raise the typed ``DraftOrderItemError`` (→ 422 in the route).

The **server computes** the draft ``subtotal`` / ``total`` on every item
mutation and discount change: each line resolves to
``unit_price_override`` if pinned, else the live product/variant price in the
draft's pinned ``currency``; ``total = max(subtotal - discount, 0)``.
Shipping is deliberately NOT part of draft totals (it resolves at
conversion). A stale line (product deleted → SET NULL, no override)
contributes 0 — visible in the response as ``line_total = null``.

A ``completed`` draft (set by conversion) is immutable: every mutating
method raises ``DraftOrderLockedError`` (→ 409).
"""

from __future__ import annotations

from sqlalchemy.orm import Query, Session, selectinload

from app.core.money import ZERO_MONEY, quantize_money, to_money
from app.crud.order import (
    VariantRequiredError,
    VariantUnavailableError,
    load_variants_for_lines,
    resolve_line_variant,
)
from app.models.draft_order import DraftOrder, DraftOrderItem, DraftOrderStatus
from app.models.product import Product
from app.models.shipping import ShippingMethod
from app.models.user import User
from app.schemas.draft_order import (
    DraftOrderCreate,
    DraftOrderItemCreate,
    DraftOrderItemUpdate,
    DraftOrderUpdate,
)


class DraftOrderLockedError(Exception):
    """A mutation targets a ``completed`` (converted) draft.

    Maps to 409 in the route — the draft is a closed working document once
    conversion has produced the real order. Mirrors the
    ``QuoteNotAcceptableError`` error posture.
    """

    def __init__(self, *, message: str = "a completed draft order cannot be modified") -> None:
        self.message = message
        super().__init__(message)


class DraftOrderItemError(Exception):
    """An item write fails a catalog precondition.

    Carries a machine ``reason`` code (``product_unavailable`` /
    ``variant_required`` / ``variant_unavailable``) the route maps to 422.
    Mirrors ``QuoteRequestError`` so the route stays a thin mapper.
    """

    def __init__(self, *, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(message)


class CRUDDraftOrder:
    """Draft-order rows + nested items (no commit; caller owns the txn)."""

    def get(self, db: Session, id: int) -> DraftOrder | None:
        """Fetch one draft with user + items (and their product/variant) eager-loaded."""
        return (
            db.query(DraftOrder)
            .options(
                selectinload(DraftOrder.user),
                selectinload(DraftOrder.items).selectinload(DraftOrderItem.product),
                selectinload(DraftOrder.items).selectinload(DraftOrderItem.variant),
            )
            .filter(DraftOrder.id == id)
            .first()
        )

    def get_multi(
        self,
        db: Session,
        *,
        status: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[DraftOrder]:
        query = self._filtered(db, status=status)
        return (
            query.options(
                selectinload(DraftOrder.user),
                selectinload(DraftOrder.items),
            )
            .order_by(DraftOrder.created_at.desc(), DraftOrder.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count(self, db: Session, *, status: str | None = None) -> int:
        return self._filtered(db, status=status).count()

    def _filtered(self, db: Session, *, status: str | None) -> Query[DraftOrder]:
        query = db.query(DraftOrder)
        if status is not None:
            query = query.filter(DraftOrder.status == status)
        return query

    def user_exists(self, db: Session, user_id: int) -> bool:
        """True if a user with ``user_id`` exists (route 404s on False)."""
        return db.query(User.id).filter(User.id == user_id).first() is not None

    def shipping_method_exists(self, db: Session, shipping_method_id: int) -> bool:
        """True if a shipping method with ``shipping_method_id`` exists (route 404s)."""
        return (
            db.query(ShippingMethod.id).filter(ShippingMethod.id == shipping_method_id).first()
            is not None
        )

    def create(self, db: Session, *, obj_in: DraftOrderCreate) -> DraftOrder:
        """Create an empty draft with a pinned currency (no lines yet).

        Flushes so the ``id`` is populated; does not commit. The caller
        validates an explicit ``user_id`` exists first (404 on miss).
        """
        draft = DraftOrder(
            currency=obj_in.currency,
            user_id=obj_in.user_id,
            guest_email=obj_in.guest_email,
            notes=obj_in.notes,
            status=DraftOrderStatus.OPEN.value,
            subtotal=ZERO_MONEY,
            total=ZERO_MONEY,
        )
        db.add(draft)
        db.flush()
        db.refresh(draft)
        return draft

    def update(self, db: Session, *, draft: DraftOrder, obj_in: DraftOrderUpdate) -> DraftOrder:
        """Apply a partial header update (no flush — the audit writer reads
        ``get_history`` between this and the caller's commit).

        Only fields explicitly sent are applied; an explicit ``null`` clears a
        nullable field. ``currency`` is immutable (the schema has no such
        field) and a ``completed`` draft raises ``DraftOrderLockedError``.
        A ``discount`` change recomputes the totals.
        """
        self._ensure_open(draft)
        data = obj_in.model_dump(exclude_unset=True)
        for field in (
            "user_id",
            "guest_email",
            "shipping_address",
            "shipping_city",
            "shipping_postal_code",
            "shipping_country",
            "shipping_method_id",
            "notes",
        ):
            if field in data:
                setattr(draft, field, data[field])
        if "discount" in data:
            draft.discount = to_money(data["discount"]) or ZERO_MONEY
        self._recompute_totals(draft)
        db.add(draft)
        return draft

    def delete(self, db: Session, *, draft: DraftOrder) -> None:
        """Delete an open draft; its items cascade (FK CASCADE + delete-orphan).

        A ``completed`` draft is a record of what got converted — deleting it
        raises ``DraftOrderLockedError`` (→ 409).
        """
        self._ensure_open(draft)
        db.delete(draft)

    # ------------------------------------------------------------------
    # Line items — every mutation recomputes subtotal/total server-side.
    # ------------------------------------------------------------------

    def add_item(
        self, db: Session, *, draft: DraftOrder, obj_in: DraftOrderItemCreate
    ) -> DraftOrderItem:
        """Add a catalog line to an open draft (no flush; caller owns the txn).

        The product must exist and be active; the variant grain follows the
        shared order-create rules (``resolve_line_variant``) — failures raise
        ``DraftOrderItemError`` (→ 422). No stock check (drafts hold no stock).
        """
        self._ensure_open(draft)
        product = (
            db.query(Product)
            .options(selectinload(Product.variants))
            .filter(Product.id == obj_in.product_id)
            .first()
        )
        if product is None or not product.is_active:
            raise DraftOrderItemError(
                reason="product_unavailable",
                message=f"Product {obj_in.product_id} is unavailable",
            )
        variants = load_variants_for_lines(
            db, [obj_in.variant_id] if obj_in.variant_id is not None else []
        )
        try:
            variant = resolve_line_variant(product, obj_in.variant_id, variants)
        except VariantRequiredError as exc:
            raise DraftOrderItemError(reason="variant_required", message=str(exc)) from exc
        except VariantUnavailableError as exc:
            raise DraftOrderItemError(reason="variant_unavailable", message=str(exc)) from exc

        item = DraftOrderItem(
            quantity=obj_in.quantity,
            unit_price_override=to_money(obj_in.unit_price_override),
        )
        # Assign relationships (not raw ids) so the effective-price/display
        # properties resolve pre-flush and the totals recompute in-memory.
        item.product = product
        item.variant = variant
        draft.items.append(item)
        self._recompute_totals(draft)
        db.add(draft)
        return item

    def update_item(
        self,
        db: Session,
        *,
        draft: DraftOrder,
        item: DraftOrderItem,
        obj_in: DraftOrderItemUpdate,
    ) -> DraftOrderItem:
        """Patch a line's quantity and/or price override (no flush).

        An explicit ``unit_price_override: null`` clears the pin back to live
        catalog pricing; omitting the field leaves it unchanged.
        """
        self._ensure_open(draft)
        data = obj_in.model_dump(exclude_unset=True)
        if data.get("quantity") is not None:
            item.quantity = data["quantity"]
        if "unit_price_override" in data:
            item.unit_price_override = to_money(data["unit_price_override"])
        self._recompute_totals(draft)
        db.add(draft)
        return item

    def remove_item(self, db: Session, *, draft: DraftOrder, item: DraftOrderItem) -> None:
        """Remove a line from an open draft and recompute the totals (no flush)."""
        self._ensure_open(draft)
        draft.items.remove(item)  # delete-orphan cascade deletes the row
        self._recompute_totals(draft)
        db.add(draft)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_open(draft: DraftOrder) -> None:
        if draft.status == DraftOrderStatus.COMPLETED.value:
            raise DraftOrderLockedError()

    @staticmethod
    def _recompute_totals(draft: DraftOrder) -> None:
        """Recompute ``subtotal`` / ``total`` from the current in-memory lines.

        ``subtotal`` = quantized sum of resolved line totals (a stale line —
        product deleted, no override — resolves to ``None`` and contributes
        0); ``total = max(subtotal - discount, 0)`` — shipping is not part of
        draft totals.
        """
        subtotal = ZERO_MONEY
        for item in draft.items:
            line_total = item.line_total
            if line_total is not None:
                subtotal += line_total
        draft.subtotal = quantize_money(subtotal)
        draft.total = max(quantize_money(draft.subtotal - draft.discount), ZERO_MONEY)


draft_order = CRUDDraftOrder()
