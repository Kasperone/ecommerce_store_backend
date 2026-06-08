"""CRUD operations for orders.

Order creation is transactional: it validates stock, snapshots product data
into ``order_items``, decrements ``product.stock``, and computes pricing in one
unit of work. The route owns the commit boundary (same pattern as the audit
writer) so the order, its items, and the stock decrement land or roll back
together.

Tax and shipping are 0.0 for now — there is no tax/shipping engine yet, so
``total == subtotal``. Documented in the foundational orders plan.
"""

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy.orm import Query, Session, selectinload

from app.crud import coupon as crud_coupon
from app.crud.base import CRUDBase
from app.crud.coupon import AppliedDiscount, CouponRedemptionError
from app.crud.inventory import inventory as crud_inventory
from app.crud.invoice import invoice as crud_invoice
from app.crud.shipping import (
    ShippingRateUnavailableError,
)
from app.crud.shipping import (
    rate as crud_shipping_rate,
)
from app.crud.warehouse import warehouse as crud_warehouse
from app.models.inventory_movement import InventoryMovementReason
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.user import User, UserRole
from app.schemas.order import OrderCreate, OrderUpdate
from app.services.dimensional_weight import billable_weight_kg, line_volume_cm3

# Forward-only status workflow for the admin PATCH endpoint. Terminal and
# stock-affecting transitions are NOT in this map — cancellation goes through
# the dedicated cancel endpoint so stock is restored, refunds (including the
# PARTIALLY_REFUNDED → REFUNDED transition) go through the refunds endpoint
# so the KOR + email + ledger fire together. Keyed by OrderStatus; str-enum
# members hash equal to their values so lookups work whether the key arrives
# as a member or a string.
_STATUS_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {OrderStatus.PAID, OrderStatus.PROCESSING},
    OrderStatus.PAID: {OrderStatus.PROCESSING, OrderStatus.SHIPPED},
    OrderStatus.PROCESSING: {OrderStatus.SHIPPED},
    OrderStatus.SHIPPED: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
    # PARTIALLY_REFUNDED → REFUNDED is the only forward path, owned by the
    # refund endpoint (admin PATCH still rejects it via
    # ``status_transition_error``).
    OrderStatus.PARTIALLY_REFUNDED: {OrderStatus.REFUNDED},
    OrderStatus.REFUNDED: set(),
}

# Statuses from which an order may still be cancelled (stock-restoring).
# PARTIALLY_REFUNDED is intentionally NOT in this set (D4): once a refund has
# been issued the only forward path is another refund (which can flip the
# status to REFUNDED), not a cancel that would lose the refund history.
_CANCELLABLE: set[OrderStatus] = {
    OrderStatus.PENDING,
    OrderStatus.PAID,
    OrderStatus.PROCESSING,
    OrderStatus.SHIPPED,
}


class ProductUnavailableError(Exception):
    """A line references a product that does not exist or is inactive."""

    def __init__(self, product_id: int) -> None:
        self.product_id = product_id
        super().__init__(f"Product {product_id} is unavailable")


class VariantRequiredError(Exception):
    """A line's product has active variants but the line carries no ``variant_id``."""

    def __init__(self, product_id: int) -> None:
        self.product_id = product_id
        super().__init__(f"Product {product_id} requires a variant selection")


class VariantUnavailableError(Exception):
    """A line references a variant that is missing, inactive, or another product's."""

    def __init__(self, variant_id: int) -> None:
        self.variant_id = variant_id
        super().__init__(f"Variant {variant_id} is unavailable")


class InsufficientStockError(Exception):
    """A line requests more units than the product has in stock."""

    def __init__(self, product_id: int, name: str, requested: int, available: int) -> None:
        self.product_id = product_id
        self.name = name
        self.requested = requested
        self.available = available
        super().__init__(
            f"Insufficient stock for '{name}': requested {requested}, available {available}"
        )


def resolve_line_variant(
    product: Product,
    variant_id: int | None,
    variants: dict[int, ProductVariant],
) -> ProductVariant | None:
    """Resolve a line's variant against the variant-selection rules.

    Returns ``None`` for a plain line on a variantless product. Raises
    ``VariantRequiredError`` when the product has active variants but the line
    carries no ``variant_id``, and ``VariantUnavailableError`` when the given
    variant is missing / inactive / another product's. Shared by the order
    create path and the quote request path (``crud_quote.request_from_cart``)
    so the rules can't drift.
    """
    if variant_id is None:
        if any(variant.is_active for variant in product.variants):
            raise VariantRequiredError(product.id)
        return None
    variant = variants.get(variant_id)
    if variant is None or variant.product_id != product.id or not variant.is_active:
        raise VariantUnavailableError(variant_id)
    return variant


def load_variants_for_lines(db: Session, variant_ids: list[int]) -> dict[int, ProductVariant]:
    """Load every referenced variant in one query (``{id: ProductVariant}``)."""
    if not variant_ids:
        return {}
    return {
        variant.id: variant
        for variant in db.query(ProductVariant).filter(ProductVariant.id.in_(variant_ids)).all()
    }


def status_transition_error(current: OrderStatus, target: OrderStatus) -> str | None:
    """Return an error message if ``current → target`` is not allowed, else None.

    A no-op transition (``current == target``) is allowed so an admin can edit
    notes/payment fields without changing status.
    """
    current = OrderStatus(current)
    target = OrderStatus(target)
    if current == target:
        return None
    if target == OrderStatus.CANCELLED:
        return "Use the cancel endpoint to cancel an order"
    if target == OrderStatus.REFUNDED or target == OrderStatus.PARTIALLY_REFUNDED:
        # Refund status (full or partial) is managed by the refund endpoint —
        # the admin PATCH never sets either directly. Mirrors the cancel
        # endpoint pattern.
        return "Use POST /api/v1/orders/{id}/refunds to issue a refund"
    if target not in _STATUS_TRANSITIONS.get(current, set()):
        return f"Cannot transition order from '{current.value}' to '{target.value}'"
    return None


class CRUDOrder(CRUDBase[Order, OrderCreate, OrderUpdate]):
    """CRUD operations for the Order model."""

    def _generate_order_number(self, db: Session) -> str:
        """Build a unique ``ORD-YYYYMMDD-XXXXXX`` order number.

        Retries on the (very unlikely) collision; raises after 10 attempts
        rather than risk an IntegrityError at commit.
        """
        date_part = datetime.now(UTC).strftime("%Y%m%d")
        for _ in range(10):
            candidate = f"ORD-{date_part}-{secrets.token_hex(3).upper()}"
            exists = db.query(Order.id).filter(Order.order_number == candidate).first()
            if exists is None:
                return candidate
        raise RuntimeError("could not generate a unique order number after 10 attempts")

    def create_with_items(self, db: Session, *, user_id: int | None, obj_in: OrderCreate) -> Order:
        """Create an order from posted line items.

        Validates availability + stock, snapshots product fields, decrements
        stock, and computes pricing. Variant lines follow the variant grain
        throughout: a product with active variants requires a ``variant_id``
        per line (else ``VariantRequiredError``), the variant must be this
        product's and active (else ``VariantUnavailableError``), availability
        is checked against the variant-scoped default-warehouse level, pricing
        comes from ``variant.get_price`` (NULL inherits the product), and the
        snapshot carries ``variant_title`` + the variant SKU. Raises
        ``ProductUnavailableError`` / ``InsufficientStockError`` /
        ``VariantRequiredError`` / ``VariantUnavailableError`` (the route maps
        all four to 422). Flushes so the ``id`` is populated; does not commit.

        ``user_id=None`` creates a **guest** order: the route guarantees
        ``obj_in.guest_email`` is present (the DB CHECK enforces it too), the
        guest fields are snapshotted, and a unique 256-bit ``guest_token`` is
        generated for account-less tracking + payment start. Authed orders
        keep every guest field NULL.
        """
        currency = obj_in.currency
        product_ids = [line.product_id for line in obj_in.items]
        products = {
            p.id: p
            for p in db.query(Product)
            .options(selectinload(Product.variants))
            .filter(Product.id.in_(product_ids))
            .all()
        }
        variants = load_variants_for_lines(
            db, [line.variant_id for line in obj_in.items if line.variant_id is not None]
        )

        # Orders fulfil from the default warehouse, so availability is checked
        # against that warehouse's level (not the product total, which may include
        # stock held in other warehouses).
        default_warehouse = crud_warehouse.get_default(db)
        if default_warehouse is None:
            raise RuntimeError("No default warehouse configured; run migrations")

        items: list[OrderItem] = []
        subtotal = 0.0
        # Validate against a local running remainder so duplicate lines for the
        # same (product, variant) pair are checked against the not-yet-committed
        # default-warehouse level. The real decrement + ledger write happens
        # after the flush (apply_delta needs order.id) — see the second loop below.
        remaining: dict[tuple[int, int | None], int] = {}
        for line in obj_in.items:
            product = products.get(line.product_id)
            if product is None or not product.is_active:
                raise ProductUnavailableError(line.product_id)
            variant = resolve_line_variant(product, line.variant_id, variants)
            key = (product.id, variant.id if variant is not None else None)
            available = remaining.get(
                key,
                crud_inventory.get_level_quantity(
                    db, product=product, warehouse_id=default_warehouse.id, variant=variant
                ),
            )
            if available < line.quantity and not product.allow_backorder:
                name = product.name if variant is None else f"{product.name} — {variant.title}"
                raise InsufficientStockError(product.id, name, line.quantity, available)
            remaining[key] = available - line.quantity
            unit_price = (
                variant.get_price(currency) if variant is not None else product.get_price(currency)
            )
            line_total = round(unit_price * line.quantity, 2)
            subtotal += line_total
            items.append(
                OrderItem(
                    product_id=product.id,
                    variant_id=variant.id if variant is not None else None,
                    product_name=product.name,
                    variant_title=variant.title if variant is not None else None,
                    product_sku=(variant.sku or product.sku)
                    if variant is not None
                    else product.sku,
                    quantity=line.quantity,
                    unit_price=unit_price,
                    total_price=line_total,
                )
            )

        subtotal = round(subtotal, 2)

        # Shipping resolution: validate the chosen method has a rate for the
        # caller's country+currency, then compute the amount via the central
        # resolver (free-above → weight-tiers → subtotal-tiers → flat). 422s
        # out the route on miss. Cart weight is summed from product.weight_kg
        # (NULL contributes 0 — legacy products with no weight skip the
        # weight-tier branch cleanly).
        #
        # Volumetric (dimensional) weight layers in here: if the resolved
        # zone has a ``dim_divisor``, the resolver's ``weight_kg`` input is
        # the **billable** weight = ``max(actual, volumetric)``. Per-line
        # volume = ``l × w × h × qty`` (any missing dim → 0, so legacy
        # un-dimensioned products contribute nothing); divisor comes from
        # the zone; max-with-actual happens here so ``apply_shipping_rate``
        # stays pure with one ``weight_kg`` input (see
        # ``app/services/dimensional_weight.py``).
        method_rate = crud_shipping_rate.resolve_one(
            db,
            country_code=obj_in.shipping_country,
            currency=currency,
            method_id=obj_in.shipping_method_id,
        )
        if method_rate is None:
            raise ShippingRateUnavailableError(
                country_code=obj_in.shipping_country,
                currency=currency,
                method_id=obj_in.shipping_method_id,
            )
        ship_method, ship_rate = method_rate
        total_weight = sum(
            (products[line.product_id].weight_kg or 0.0) * line.quantity for line in obj_in.items
        )
        total_volume_cm3 = sum(
            line_volume_cm3(
                products[line.product_id].length_cm,
                products[line.product_id].width_cm,
                products[line.product_id].height_cm,
                line.quantity,
            )
            for line in obj_in.items
        )
        billable_kg = billable_weight_kg(total_weight, total_volume_cm3, ship_rate.zone.dim_divisor)
        shipping_amount = round(
            crud_shipping_rate.apply_shipping_rate(ship_rate, subtotal, weight_kg=billable_kg),
            2,
        )

        # Discount resolution: ``pick_best`` is the single source of truth for
        # which coupon (typed code vs active automatics) an order gets — the
        # validate-preview endpoint routes through the same picker, so the
        # preview can never diverge. It runs AFTER shipping_amount is known
        # because a free_shipping coupon's saving is the shipping line. An
        # invalid typed code re-raises CouponRedemptionError (400 at the route
        # — never silently falls back to an automatic); an automatic winner
        # whose ``redeem`` loses the last-use race is dropped and the pick is
        # retried without it (the customer never asked for it, so it must not
        # fail the checkout). The used_count bump lands in the same unit of
        # work as the order, so a rolled-back order never burns a use.
        applied: AppliedDiscount | None = None
        excluded: set[int] = set()
        while True:
            applied = crud_coupon.pick_best(
                db,
                code=obj_in.coupon_code,
                subtotal=subtotal,
                shipping=shipping_amount,
                currency=currency,
                user_id=user_id,
                exclude_coupon_ids=excluded,
            )
            if applied is None:
                break
            try:
                crud_coupon.redeem(db, coupon=applied.coupon)
            except CouponRedemptionError:
                if applied.is_manual:
                    raise
                excluded.add(applied.coupon.id)
                continue
            break

        # percent/fixed discount the goods subtotal; free_shipping zeroes the
        # shipping line instead (order.discount stays 0 so the documented
        # invariant ``total == round(subtotal - discount + shipping, 2)`` holds
        # and the invoice/CSV render a 0.00 shipping line, no bogus discount row).
        discount = 0.0
        redeemed_coupon = applied.coupon if applied is not None else None
        if applied is not None:
            if applied.free_shipping:
                shipping_amount = 0.0
            else:
                discount = applied.saving

        total = round(subtotal - discount + shipping_amount, 2)

        is_guest = user_id is None
        order = Order(
            user_id=user_id,
            guest_email=obj_in.guest_email if is_guest else None,
            # Opaque 256-bit tracking token, guest orders only (authed orders
            # keep NULL — their account is the credential).
            guest_token=secrets.token_urlsafe(32) if is_guest else None,
            guest_locale=obj_in.locale if is_guest else None,
            order_number=self._generate_order_number(db),
            status=OrderStatus.PENDING,
            subtotal=subtotal,
            tax=0.0,
            shipping=shipping_amount,
            discount=discount,
            total=total,
            currency=currency,
            coupon_id=redeemed_coupon.id if redeemed_coupon is not None else None,
            coupon_code=redeemed_coupon.code if redeemed_coupon is not None else None,
            shipping_address=obj_in.shipping_address,
            shipping_city=obj_in.shipping_city,
            shipping_postal_code=obj_in.shipping_postal_code,
            shipping_country=obj_in.shipping_country,
            shipping_method_id=ship_method.id,
            shipping_method_name=ship_method.name,
            notes=obj_in.notes,
            items=items,
        )
        db.add(order)
        db.flush()  # populate order.id before writing inventory movements

        # Record the per-user redemption now that order.id exists (same unit of
        # work as redeem() above, so a rolled-back order burns no use and records
        # no redemption). Backs Coupon.per_user_limit. Skipped for guest orders:
        # ``CouponRedemption.user_id`` is NOT NULL, so ``per_user_limit`` is
        # unenforceable for guests (documented limitation) — the global
        # ``max_uses`` cap still applies via ``redeem`` above.
        if redeemed_coupon is not None and user_id is not None:
            crud_coupon.record_redemption(
                db, coupon_id=redeemed_coupon.id, user_id=user_id, order_id=order.id
            )

        # Decrement stock + append the ledger now that order.id exists; one
        # ORDER_PLACED movement per line (duplicate lines decrement cumulatively).
        # Variant lines decrement at variant grain (apply_delta maintains
        # variant.stock + product.stock + the level in lock-step).
        for line in obj_in.items:
            crud_inventory.apply_delta(
                db,
                product=products[line.product_id],
                delta=-line.quantity,
                reason=InventoryMovementReason.ORDER_PLACED,
                warehouse_id=default_warehouse.id,
                variant=variants.get(line.variant_id) if line.variant_id is not None else None,
                actor_user_id=user_id,
                order_id=order.id,
                allow_negative=products[line.product_id].allow_backorder,
            )

        # Flush the stock decrements + movement rows so the unit of work is
        # consistent before returning (the route owns the commit). Explicit
        # because the session may have autoflush disabled.
        db.flush()
        db.refresh(order)
        return order

    def cancel(self, db: Session, *, order: Order, actor_user_id: int | None = None) -> Order:
        """Restore stock for every line and mark the order cancelled.

        Writes one ORDER_CANCELLED inventory movement per line via
        ``crud_inventory.apply_delta``; ``actor_user_id`` attributes the movement
        to the cancelling user (None when called outside a request, e.g. tests).
        Stock is restored to the default warehouse (where orders are fulfilled
        from). Variant lines restore at variant grain; a since-deleted variant
        (``variant_id`` SET NULL, ``variant_title`` snapshot kept) restores at
        product grain with an explanatory note. Caller is responsible for the
        cancellable-status check and the commit.
        """
        default_warehouse = crud_warehouse.get_default(db)
        for item in order.items:
            product = db.get(Product, item.product_id)
            if product is not None:
                variant = (
                    db.get(ProductVariant, item.variant_id) if item.variant_id is not None else None
                )
                note = None
                if variant is None and item.variant_title is not None:
                    note = f"variant '{item.variant_title}' deleted; restored at product grain"
                crud_inventory.apply_delta(
                    db,
                    product=product,
                    delta=item.quantity,
                    reason=InventoryMovementReason.ORDER_CANCELLED,
                    warehouse_id=default_warehouse.id if default_warehouse else None,
                    variant=variant,
                    actor_user_id=actor_user_id,
                    order_id=order.id,
                    note=note,
                )
        order.status = OrderStatus.CANCELLED
        db.add(order)
        return order

    def is_cancellable(self, order: Order) -> bool:
        return OrderStatus(order.status) in _CANCELLABLE

    def apply_admin_update(self, db: Session, *, order: Order, obj_in: OrderUpdate) -> Order:
        """Apply an admin PATCH (status workflow + notes/payment/tracking fields).

        Coerces ``status`` to a real ``OrderStatus`` member (the schema's
        ``use_enum_values`` would otherwise hand us a raw string that the
        SQLEnum column may reject). Tracking fields are typically set alongside
        ``status=shipped`` but may be edited on their own afterwards (a no-op
        status keeps the PATCH valid). Does not flush — the audit writer reads
        ``get_history`` before the route commits.
        """
        update_data = obj_in.model_dump(exclude_unset=True)
        if "status" in update_data and obj_in.status is not None:
            new_status = OrderStatus(obj_in.status)
            # Stamp the delivery timestamp on the transition INTO delivered —
            # it anchors the returns-eligibility window. A no-op
            # delivered→delivered PATCH must not move it.
            if (
                new_status == OrderStatus.DELIVERED
                and OrderStatus(order.status) != OrderStatus.DELIVERED
            ):
                order.delivered_at = datetime.now(UTC).replace(tzinfo=None)
            order.status = new_status
        for field in (
            "payment_method",
            "payment_intent_id",
            "notes",
            "shipping_carrier",
            "tracking_number",
            "tracking_url",
        ):
            if field in update_data:
                setattr(order, field, update_data[field])
        db.add(order)
        return order

    def mark_paid(
        self,
        db: Session,
        *,
        order: Order,
        payment_intent_id: str,
        payment_method: str,
    ) -> Order:
        """Idempotently flip a pending order to paid (payment-provider webhook).

        No-op when the order isn't pending, so duplicate/late notifications are
        safe. Records the provider's payment id + method (``"payu"`` / ``"stripe"``).
        Issues the sequential faktura number in the same transaction as the flip
        (gap-free — the number and the paid state commit or roll back together).
        Caller owns the commit.
        """
        if OrderStatus(order.status) != OrderStatus.PENDING:
            return order
        order.status = OrderStatus.PAID
        order.payment_method = payment_method
        order.payment_intent_id = payment_intent_id
        db.add(order)
        crud_invoice.issue_for_order(db, order=order)
        return order

    def get_by_order_number(self, db: Session, *, order_number: str) -> Order | None:
        """Look up an order by its public ``order_number``.

        Used by the unauthenticated PayU webhook, which echoes our order_number
        back as ``extOrderId``.
        """
        return (
            db.query(Order)
            .options(selectinload(Order.items))
            .filter(Order.order_number == order_number)
            .first()
        )

    def get_by_guest_token(self, db: Session, *, token: str) -> Order | None:
        """Look up a guest order by its opaque tracking token.

        Backs the public ``GET /orders/track/{token}`` view (plain 404 on a
        miss — no enumeration signal). The token column is unique-indexed and
        only ever populated on guest orders, so an authed order can never be
        reached this way.
        """
        return (
            db.query(Order)
            .options(selectinload(Order.items))
            .filter(Order.guest_token == token)
            .first()
        )

    def get_scoped(self, db: Session, *, order_id: int, user: User) -> Order | None:
        """Fetch one order visible to ``user``.

        Customers see only their own; admins see any. Returns None for both
        not-found and not-owned so the route can 404 without leaking existence.
        Eager-loads ``refunds`` (+ actor + KOR) so the response carries the full
        admin/customer view in one query — see ``app/crud/refund.py``.
        """
        # Import inside the method: ``crud_order`` is imported by
        # ``crud_refund`` for ``REFUNDABLE_STATUSES`` (via the route layer) —
        # a module-level import here would close the cycle.
        from app.crud.refund import order_refunds_load_options

        query = (
            db.query(Order)
            .options(
                selectinload(Order.items),
                selectinload(Order.user),
                *order_refunds_load_options(),
            )
            .filter(Order.id == order_id)
        )
        if user.role != UserRole.ADMIN:
            query = query.filter(Order.user_id == user.id)
        return query.first()

    def _apply_scope_and_filters(
        self,
        query: Query[Order],
        *,
        user: User,
        status: OrderStatus | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> Query[Order]:
        """Apply customer/admin scoping + status + ``created_at`` date-range filters.

        Shared by the scoped list, its count, and the CSV exporter so the three
        never drift ("export = what you see"). The date range bounds ``created_at``
        by UTC calendar day with ``date_to`` inclusive (``< day after date_to``);
        ``created_at`` is naive UTC, so the boundaries are UTC days, not the
        admin's local timezone.
        """
        if user.role != UserRole.ADMIN:
            query = query.filter(Order.user_id == user.id)
        if status is not None:
            query = query.filter(Order.status == status)
        if date_from is not None:
            query = query.filter(Order.created_at >= datetime.combine(date_from, time.min))
        if date_to is not None:
            query = query.filter(
                Order.created_at < datetime.combine(date_to, time.min) + timedelta(days=1)
            )
        return query

    def get_multi_scoped(
        self,
        db: Session,
        *,
        user: User,
        status: OrderStatus | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> list[Order]:
        query = db.query(Order).options(selectinload(Order.items), selectinload(Order.user))
        query = self._apply_scope_and_filters(
            query, user=user, status=status, date_from=date_from, date_to=date_to
        )
        return (
            query.order_by(Order.created_at.desc(), Order.id.desc()).offset(skip).limit(limit).all()
        )

    def count_scoped(
        self,
        db: Session,
        *,
        user: User,
        status: OrderStatus | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> int:
        query = db.query(Order)
        query = self._apply_scope_and_filters(
            query, user=user, status=status, date_from=date_from, date_to=date_to
        )
        return query.count()

    def get_for_export(
        self,
        db: Session,
        *,
        user: User,
        status: OrderStatus | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[Order]:
        """Load every order matching the scope/filters (no pagination) for CSV export.

        Same filters as the scoped list so the export mirrors the on-screen view.
        Eager-loads ``items`` + ``user`` (the exporter packs line items and the
        customer name/email per row). MVP loads all rows into memory — fine for
        low-thousands; streaming via ``yield_per`` + ``StreamingResponse`` is a
        noted future optimization.
        """
        query = db.query(Order).options(selectinload(Order.items), selectinload(Order.user))
        query = self._apply_scope_and_filters(
            query, user=user, status=status, date_from=date_from, date_to=date_to
        )
        return query.order_by(Order.created_at.desc(), Order.id.desc()).all()


order = CRUDOrder(Order)
