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

import secrets

from sqlalchemy.orm import Query, Session, selectinload

from app.core.money import ZERO_MONEY, quantize_money, to_money
from app.crud.inventory import inventory as crud_inventory
from app.crud.order import (
    InPostPointRequiredError,
    OrderLineSpec,
    VariantRequiredError,
    VariantUnavailableError,
    build_order_items,
    load_variants_for_lines,
    resolve_line_variant,
    resolve_order_shipping,
)
from app.crud.order import order as crud_order
from app.crud.shipping import ShippingRateUnavailableError
from app.crud.warehouse import warehouse as crud_warehouse
from app.models.draft_order import DraftOrder, DraftOrderItem, DraftOrderStatus
from app.models.inventory_movement import InventoryMovementReason
from app.models.order import Order, OrderStatus
from app.models.product import Product
from app.models.shipping import ShippingMethod
from app.models.user import User
from app.schemas.draft_order import (
    DraftOrderCreate,
    DraftOrderItemCreate,
    DraftOrderItemUpdate,
    DraftOrderUpdate,
)
from app.services import tax as tax_service
from app.services.order_math import recompute_totals


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


class DraftOrderNotConvertibleError(Exception):
    """A draft order fails a convert eligibility precondition.

    Carries a machine ``reason`` code + an HTTP ``status_code`` the route maps
    to the response (422 for state/data preconditions, 409 for
    already-converted). Mirrors ``QuoteNotConvertibleError`` so the route
    stays a thin mapper.
    """

    def __init__(self, *, reason: str, message: str, status_code: int) -> None:
        self.reason = reason
        self.message = message
        self.status_code = status_code
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
    # Conversion — draft → real pending order (mirrors CRUDQuote.convert_to_order)
    # ------------------------------------------------------------------

    def convert_to_order(
        self,
        db: Session,
        *,
        draft: DraftOrder,
        payment_method: str,
        mark_as_paid: bool,
        actor_user_id: int,
    ) -> Order:
        """Convert an open draft into a real order (no commit; caller owns the txn).

        Attribution follows the draft's owner: the customer (``user_id``) or a
        guest (``guest_email`` — a fresh ``guest_token`` is generated exactly
        like the storefront guest path; ``guest_locale`` stays NULL, the email
        layer falls back to "en"). ``actor_user_id`` is only the audit actor,
        threaded into the inventory movements.

        Per line: honours ``unit_price_override`` when pinned (does NOT
        re-price), else prices from the live catalog at the draft's
        ``currency``; snapshots ``product_name``/``product_sku``/``vat_rate``
        via the shared ``build_order_items`` and decrements stock from the
        default warehouse exactly like checkout (one ORDER_PLACED movement per
        line). Shipping resolves for real via ``resolve_order_shipping``
        (rate + dimensional weight); the manual ``discount`` is clamped to the
        subtotal; ``total = quantize_money(subtotal - discount + shipping)``.

        Eligibility is enforced here (raises ``DraftOrderNotConvertibleError``
        with a machine reason + status code) so the route is a thin mapper:
        - not ``open`` or already converted → 409 ``already_converted``
        - no items → 422 ``no_items``
        - any line with ``product_id IS NULL`` (product deleted) → 422 ``stale_line``
        - no owner (``user_id`` NULL and ``guest_email`` blank) → 422 ``no_owner``
        - any blank shipping-address field → 422 ``missing_address``
        - ``shipping_method_id`` NULL → 422 ``missing_shipping_method``
        - variant missing/inactive/required-but-unset → 422 ``variant_unavailable``
        - no rate for the draft's country+currency → 422 ``shipping_unavailable``
        - InPost locker method → 422 ``unsupported_method`` (drafts carry no
          Paczkomat point)
        - product missing/inactive or insufficient stock → the shared
          ``ProductUnavailableError`` / ``InsufficientStockError`` propagate
          (the route maps both to 422, like admin quotes).

        With ``mark_as_paid`` the order is flipped to PAID via
        ``crud_order.mark_paid`` in the same transaction — this issues the
        gap-free faktura (rollback-safe). The draft is then marked
        ``completed`` + ``converted_order_id`` set, deliberately LAST and
        without a later flush: ``mark_paid`` → ``issue_for_order`` flushes,
        which would clear the draft's ``get_history`` before the route's
        audit ``record_update`` reads it.
        """
        if draft.status != DraftOrderStatus.OPEN.value or draft.converted_order_id is not None:
            raise DraftOrderNotConvertibleError(
                reason="already_converted",
                message="draft order already converted",
                status_code=409,
            )
        if not draft.items:
            raise DraftOrderNotConvertibleError(
                reason="no_items",
                message="cannot convert a draft order with no line items",
                status_code=422,
            )
        if any(item.product_id is None for item in draft.items):
            raise DraftOrderNotConvertibleError(
                reason="stale_line",
                message="a line references a deleted product; remove it before converting",
                status_code=422,
            )
        if draft.user_id is None and not (draft.guest_email or "").strip():
            raise DraftOrderNotConvertibleError(
                reason="no_owner",
                message="set a customer or a guest email before converting",
                status_code=422,
            )
        if not all(
            (getattr(draft, field) or "").strip()
            for field in (
                "shipping_address",
                "shipping_city",
                "shipping_postal_code",
                "shipping_country",
            )
        ):
            raise DraftOrderNotConvertibleError(
                reason="missing_address",
                message="set a complete shipping address before converting",
                status_code=422,
            )
        if draft.shipping_method_id is None:
            raise DraftOrderNotConvertibleError(
                reason="missing_shipping_method",
                message="set a shipping method before converting",
                status_code=422,
            )

        default_warehouse = crud_warehouse.get_default(db)
        if default_warehouse is None:
            raise RuntimeError("No default warehouse configured; run migrations")

        # Resolve products in one query (quote-convert pattern); the stale_line
        # guard above guarantees every line has a product_id.
        product_ids = [item.product_id for item in draft.items]
        products = {
            p.id: p
            for p in db.query(Product)
            .options(selectinload(Product.variants))
            .filter(Product.id.in_(product_ids))
            .all()
        }
        variants = load_variants_for_lines(
            db, [item.variant_id for item in draft.items if item.variant_id is not None]
        )
        line_specs = []
        for item in draft.items:
            assert item.product_id is not None  # noqa: S101 -- guarded by the stale_line check above
            line_specs.append(
                OrderLineSpec(
                    product_id=item.product_id,
                    variant_id=item.variant_id,
                    quantity=item.quantity,
                    # None override → live catalog price at draft.currency.
                    unit_price=item.unit_price_override,
                    line_key=item.id,
                )
            )
        # Reverse charge (Phase-0 tax U4): resolved from the draft's owner +
        # destination, same rule as checkout. A guest draft or a domestic/non-EU
        # destination stays domestic.
        tax_ctx = tax_service.resolve_tax_context(
            db, user_id=draft.user_id, shipping_country=draft.shipping_country
        )
        try:
            order_items, subtotal, line_variants = build_order_items(
                db,
                line_specs=line_specs,
                products=products,
                variants=variants,
                default_warehouse_id=default_warehouse.id,
                currency=draft.currency,
                tax_ctx=tax_ctx,
            )
        except (VariantRequiredError, VariantUnavailableError) as exc:
            raise DraftOrderNotConvertibleError(
                reason="variant_unavailable",
                message=str(exc),
                status_code=422,
            ) from exc

        # Real shipping resolution (unlike quotes, which defer it): the shared
        # helper applies the rate + dimensional weight. Drafts carry no InPost
        # Paczkomat point, so a locker method's point-required error maps to
        # unsupported_method rather than asking for a point we can't collect.
        assert draft.shipping_country is not None  # noqa: S101 -- guarded by the missing_address check above
        try:
            shipping_amount, ship_method = resolve_order_shipping(
                db,
                product_lines=[
                    (products[item.product_id], item.quantity)
                    for item in draft.items
                    if item.product_id is not None
                ],
                subtotal=subtotal,
                country=draft.shipping_country,
                currency=draft.currency,
                shipping_method_id=draft.shipping_method_id,
                inpost_point=None,
            )
        except ShippingRateUnavailableError as exc:
            raise DraftOrderNotConvertibleError(
                reason="shipping_unavailable",
                message=str(exc),
                status_code=422,
            ) from exc
        except InPostPointRequiredError as exc:
            raise DraftOrderNotConvertibleError(
                reason="unsupported_method",
                message="InPost locker methods are not supported for draft-order "
                "conversion (a draft carries no Paczkomat point)",
                status_code=422,
            ) from exc

        discount = min(draft.discount, subtotal)
        total = quantize_money(subtotal - discount + shipping_amount)

        is_guest = draft.user_id is None
        order = Order(
            user_id=draft.user_id,
            guest_email=draft.guest_email if is_guest else None,
            # Opaque 256-bit tracking token, guest orders only (authed orders
            # keep NULL — their account is the credential). Mirrors checkout.
            guest_token=secrets.token_urlsafe(32) if is_guest else None,
            guest_locale=None,
            order_number=crud_order._generate_order_number(db),
            status=OrderStatus.PENDING,
            subtotal=subtotal,
            tax=ZERO_MONEY,
            shipping=shipping_amount,
            discount=discount,
            total=total,
            currency=draft.currency,
            shipping_address=draft.shipping_address,
            shipping_city=draft.shipping_city,
            shipping_postal_code=draft.shipping_postal_code,
            shipping_country=draft.shipping_country,
            shipping_method_id=ship_method.id,
            shipping_method_name=ship_method.name,
            payment_method=payment_method,
            tax_treatment=tax_ctx.treatment,
            buyer_vat_id=tax_ctx.buyer_vat_id,
            buyer_vies_confirmed_at=tax_ctx.buyer_vies_confirmed_at,
            notes=f"Converted from draft order #{draft.id}",
            items=order_items,
        )
        db.add(order)
        db.flush()  # populate order.id before writing inventory movements

        for item in draft.items:
            assert item.product_id is not None  # noqa: S101 -- guarded by the stale_line check above
            product = products[item.product_id]
            crud_inventory.apply_delta(
                db,
                product=product,
                delta=-item.quantity,
                reason=InventoryMovementReason.ORDER_PLACED,
                warehouse_id=default_warehouse.id,
                variant=line_variants.get(item.id),
                actor_user_id=actor_user_id,
                order_id=order.id,
                allow_negative=product.allow_backorder,
            )

        if mark_as_paid:
            # Same-transaction PENDING→PAID flip + gap-free faktura issue.
            # issue_for_order flushes — hence this runs BEFORE the draft
            # marker mutation below (see the docstring).
            crud_order.mark_paid(
                db,
                order=order,
                payment_intent_id=f"manual-draft-{draft.id}",
                payment_method=payment_method,
            )

        # Set the conversion markers but do NOT flush afterwards: the route's
        # ``record_update(resource_type="draft_order")`` reads ``get_history``
        # (cleared by flush) to diff the change, and the route owns the commit.
        # ``order`` is already flushed (above) so its id + items are populated.
        draft.status = DraftOrderStatus.COMPLETED.value
        draft.converted_order_id = order.id
        db.add(draft)
        return order

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

        Delegates the arithmetic to the shared ``services.order_math.recompute_totals``
        (the same seam real pending-order edits use). A stale line (product
        deleted, no override) resolves to ``None`` and contributes 0; shipping
        is not part of draft totals (``shipping=0``).
        """
        draft.subtotal, draft.total = recompute_totals(
            line_totals=[item.line_total for item in draft.items],
            discount=draft.discount,
        )


draft_order = CRUDDraftOrder()
