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

import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy.orm import Query, Session, selectinload

from app.core.config import settings
from app.core.money import ZERO_MONEY, net_of_gross, quantize_money
from app.crud import coupon as crud_coupon
from app.crud.base import CRUDBase
from app.crud.coupon import AppliedDiscount, CouponRedemptionError
from app.crud.gift_card import GiftCardError
from app.crud.gift_card import gift_card as crud_gift_card
from app.crud.inventory import inventory as crud_inventory
from app.crud.invoice import invoice as crud_invoice
from app.crud.order_event import order_event as crud_order_event
from app.crud.price_list import price_list as crud_price_list
from app.crud.shipping import (
    ShippingRateUnavailableError,
)
from app.crud.shipping import (
    rate as crud_shipping_rate,
)
from app.crud.store_credit import store_credit as crud_store_credit
from app.crud.warehouse import warehouse as crud_warehouse
from app.models.gift_card import GiftCard
from app.models.inventory_movement import InventoryMovementReason
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.shipping import ShippingMethod
from app.models.user import User, UserRole
from app.schemas.order import OrderCreate, OrderUpdate
from app.services import tax as tax_service
from app.services.dimensional_weight import billable_weight_kg, line_volume_cm3
from app.services.order_math import recompute_totals

logger = logging.getLogger(__name__)

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


class InPostPointRequiredError(Exception):
    """An InPost-locker shipping method was chosen without a Paczkomat point."""

    def __init__(self) -> None:
        super().__init__("Select an InPost Paczkomat point for this shipping method")


class ShippingRequiredError(Exception):
    """A physical line is present but the checkout supplied no shipping details.

    All-gift-card carts skip shipping (v2); any physical (non-gift-card) line
    makes ``shipping_method_id`` + a destination country + an address mandatory.
    The route maps this to 422.
    """

    def __init__(self) -> None:
        super().__init__("This order needs a shipping method and address")


class NetTermsUnavailableError(Exception):
    """Pay-on-account was requested by a caller with no active net-terms company.

    The route maps this to 409 ``net_terms_unavailable``.
    """

    def __init__(self) -> None:
        super().__init__("Net terms (pay on account) is not available for this account")


class NetTermsCurrencyMismatchError(Exception):
    """A credit-limited company was billed in a currency other than its own.

    Credit is tracked in ``company.default_currency``; a pay-on-account order in a
    different currency can't be measured against the limit. Route → 409
    ``net_terms_currency_mismatch``.
    """

    def __init__(self) -> None:
        super().__init__("Net terms orders must use the company's billing currency")


class CreditLimitExceededError(Exception):
    """Outstanding balance + this order would exceed the company's credit limit.

    Route → 409 ``credit_limit_exceeded``. Card/PayU/COD remain available; only
    the pay-on-account option is blocked.
    """

    def __init__(self) -> None:
        super().__init__("This order would exceed the company's credit limit")


def is_inpost_locker_method(method: ShippingMethod) -> bool:
    """True when a shipping method is an InPost parcel-locker service.

    Keyed on the admin-set ``code`` convention (``inpost_paczkomat`` /
    ``inpost_locker`` …) — the storefront shows the Geowidget point-picker for
    these and the order must carry the chosen point.
    """
    return bool(method.code) and method.code.lower().startswith("inpost")


class OrderNotEditableError(Exception):
    """An edit was attempted on a non-PENDING order (paid/fulfilled/refunded).

    The immutable-order ledger is deliberate: a paid order has a faktura and a
    settled amount, so line changes go through the refund / corrective-invoice
    path, never an in-place recompute. v1 refuses to mutate anything but a
    PENDING order's items.
    """

    def __init__(self, status: OrderStatus) -> None:
        self.status = OrderStatus(status)
        super().__init__(f"Order is not editable in status '{self.status.value}'")


class OrderShippingCountryMissingError(Exception):
    """Shipping had to be re-resolved on an order that has no destination country.

    Only reachable for quote-converted orders, whose address is set by an admin
    at fulfilment. Shipping rates are country-keyed, so there is nothing honest
    to resolve against — the admin must set the address first.
    """

    def __init__(self, order_id: int) -> None:
        self.order_id = order_id
        super().__init__(
            f"Order #{order_id} has no shipping country — set the shipping address "
            "before changing items or the shipping method."
        )


class OrderTenderReconciliationError(Exception):
    """An edit left ``amount_due`` negative — the tender ledger and the order disagree.

    Editing an order downward releases the stored-value excess back to the card
    / credit account and rewrites the snapshots by **what actually moved**
    (SEC-019). If the ledger holds less than the snapshot claims, the release
    comes up short and the order would be left owing a negative amount, which no
    provider webhook can ever match — ``_paid_amount_matches`` computes
    ``round(amount_due * 100)`` — so the order becomes unsettleable.

    That is a data-integrity fault, not a user error: it means the snapshot was
    written without a matching ledger row. Failing the edit loudly keeps the
    order in its previous, payable state instead of silently wedging it.
    """

    def __init__(self, order_id: int, amount_due: Decimal) -> None:
        self.order_id = order_id
        self.amount_due = amount_due
        super().__init__(
            f"Order #{order_id} would be left with a negative amount due ({amount_due}): "
            "its gift-card / store-credit snapshot exceeds what the tender ledger holds."
        )


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


@dataclass(frozen=True)
class OrderLineSpec:
    """One catalog line to build into an ``OrderItem``.

    ``unit_price=None`` means "price from the live catalog at ``currency``"
    (the storefront checkout behavior); a concrete ``Decimal`` pins the line to
    a negotiated/overridden price (quote conversion, admin draft orders).
    ``line_key`` is an opaque caller identifier echoed back in the resolved-
    variants map (quote conversion keys it by quote-line id for the later
    inventory pass).
    """

    product_id: int
    variant_id: int | None
    quantity: int
    unit_price: Decimal | None = None
    line_key: int | None = None


def build_order_items(
    db: Session,
    *,
    line_specs: Sequence[OrderLineSpec],
    products: dict[int, Product],
    variants: dict[int, ProductVariant],
    default_warehouse_id: int,
    currency: str,
    tax_ctx: tax_service.TaxContext | None = None,
) -> tuple[list[OrderItem], Decimal, dict[int | None, ProductVariant | None]]:
    """The stock-check + snapshot loop shared by checkout and admin conversions.

    For each line: validates the product is live, resolves the variant grain
    (``VariantRequiredError`` / ``VariantUnavailableError``), checks
    default-warehouse availability against a local running remainder (so
    duplicate lines for the same (product, variant) pair are checked against
    the not-yet-committed level; ``allow_backorder`` bypasses), prices it
    (catalog price at ``currency`` unless the spec pins ``unit_price``), and
    snapshots the ``OrderItem`` — including ``vat_rate`` from the product, so
    per-line VAT is always faktura-correct regardless of the calling path.

    ``tax_ctx`` decides the treatment (default DOMESTIC = today's behaviour). Under
    **reverse charge** each unit's gross price is decomposed to net at the
    product's home-country rate (the VAT baked into the catalog/pinned price is
    stripped), the line snapshots ``vat_rate=0``, and ``unit_price x quantity ==
    total_price`` still holds exactly. The buyer pays net; the store charges no VAT.

    Returns ``(items, subtotal, resolved_variants_by_line_key)``. The caller
    owns the later stock decrement (``apply_delta`` needs ``order.id``, which
    only exists post-flush).
    """
    ctx = tax_ctx or tax_service.TaxContext.domestic()
    items: list[OrderItem] = []
    subtotal = ZERO_MONEY
    resolved: dict[int | None, ProductVariant | None] = {}
    remaining: dict[tuple[int, int | None], int] = {}
    # The catalog gross prices carry the store's home-country VAT, so the rate is
    # always resolved at the home country (single-jurisdiction / fixed-gross model).
    # Reverse charge doesn't change *which* rate — it strips that rate out to net.
    tax_country = settings.TAX_HOME_COUNTRY
    rate_cache: dict[int, int] = {}
    for spec in line_specs:
        product = products.get(spec.product_id)
        if product is None or not product.is_active:
            raise ProductUnavailableError(spec.product_id)
        variant = resolve_line_variant(product, spec.variant_id, variants)
        resolved[spec.line_key] = variant
        key = (product.id, variant.id if variant is not None else None)
        available = remaining.get(
            key,
            crud_inventory.get_level_quantity(
                db, product=product, warehouse_id=default_warehouse_id, variant=variant
            ),
        )
        if available < spec.quantity and not product.allow_backorder:
            name = product.name if variant is None else f"{product.name} — {variant.title}"
            raise InsufficientStockError(product.id, name, spec.quantity, available)
        remaining[key] = available - spec.quantity
        gross_unit = spec.unit_price
        if gross_unit is None:
            gross_unit = (
                variant.get_price(currency) if variant is not None else product.get_price(currency)
            )
        category = product.tax_category
        if category.id not in rate_cache:
            rate_cache[category.id] = tax_service.resolve_rate_int(
                db, category=category, country_code=tax_country
            )
        domestic_rate = rate_cache[category.id]
        if product.is_gift_card:
            # A gift-card sale is not a taxable supply (MPV voucher — VAT lands
            # on the goods bought with the card, not on the card itself). The
            # face value IS the price, so nothing is stripped; the line snapshots
            # 0% and shows in the faktura's 0% group. Takes precedence over
            # reverse charge — a voucher is untaxed under either treatment.
            unit_price = gross_unit
            vat_rate = 0
        elif ctx.is_reverse_charge:
            # Strip the home VAT: the buyer pays net, the line snapshots 0% so the
            # faktura shows a single 0% group (compute_vat_breakdown is unchanged).
            unit_price = net_of_gross(gross_unit, domestic_rate)
            vat_rate = 0
        else:
            unit_price = gross_unit
            vat_rate = domestic_rate
        line_total = quantize_money(unit_price * spec.quantity)
        subtotal += line_total
        items.append(
            OrderItem(
                product_id=product.id,
                variant_id=variant.id if variant is not None else None,
                product_name=product.name,
                variant_title=variant.title if variant is not None else None,
                product_sku=(variant.sku or product.sku) if variant is not None else product.sku,
                quantity=spec.quantity,
                unit_price=unit_price,
                total_price=line_total,
                vat_rate=vat_rate,
                is_gift_card=product.is_gift_card,
            )
        )
    return items, quantize_money(subtotal), resolved


def resolve_order_shipping(
    db: Session,
    *,
    product_lines: Sequence[tuple[Product, int]],
    subtotal: Decimal,
    country: str,
    currency: str,
    shipping_method_id: int,
    inpost_point: str | None = None,
) -> tuple[Decimal, ShippingMethod]:
    """The shipping-method/rate/billable-weight resolution shared by checkout
    and admin conversions (draft orders).

    Validates the chosen method has a rate for ``country``+``currency``
    (raises ``ShippingRateUnavailableError`` on miss), enforces the InPost
    locker point requirement (``InPostPointRequiredError`` when the method is
    a locker service and ``inpost_point`` is blank), and computes the amount
    via the central resolver (free-above → weight-tiers → subtotal-tiers →
    flat). ``product_lines`` is ``(product, quantity)`` per line — weight is
    summed from ``product.weight_kg`` (NULL contributes 0 — legacy products
    with no weight skip the weight-tier branch cleanly).

    Volumetric (dimensional) weight layers in here: if the resolved zone has
    a ``dim_divisor``, the resolver's ``weight_kg`` input is the **billable**
    weight = ``max(actual, volumetric)``. Per-line volume = ``l x w x h x
    qty`` (any missing dim → 0, so legacy un-dimensioned products contribute
    nothing); divisor comes from the zone; max-with-actual happens here so
    ``apply_shipping_rate`` stays pure with one ``weight_kg`` input (see
    ``app/services/dimensional_weight.py``).

    Returns ``(quantized amount, ShippingMethod)``.
    """
    method_rate = crud_shipping_rate.resolve_one(
        db,
        country_code=country,
        currency=currency,
        method_id=shipping_method_id,
    )
    if method_rate is None:
        raise ShippingRateUnavailableError(
            country_code=country,
            currency=currency,
            method_id=shipping_method_id,
        )
    ship_method, ship_rate = method_rate
    # InPost lockers require a chosen Paczkomat point (the storefront
    # Geowidget supplies it); reject a locker order without one.
    if is_inpost_locker_method(ship_method) and not (inpost_point or "").strip():
        raise InPostPointRequiredError()
    total_weight = sum((product.weight_kg or 0.0) * quantity for product, quantity in product_lines)
    total_volume_cm3 = sum(
        line_volume_cm3(product.length_cm, product.width_cm, product.height_cm, quantity)
        for product, quantity in product_lines
    )
    billable_kg = billable_weight_kg(total_weight, total_volume_cm3, ship_rate.zone.dim_divisor)
    amount = quantize_money(
        crud_shipping_rate.apply_shipping_rate(ship_rate, subtotal, weight_kg=billable_kg),
    )
    return amount, ship_method


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

        # B2B price list (Shopify-gap P2 #14): if the buyer is an active member
        # of a company with an active price list, each line for a product that
        # has an entry is priced from the list (variant-grain wins over
        # product-grain; no entry → catalog price). This reuses the existing
        # pinned-``unit_price`` mechanism — a resolved list price pins the line,
        # ``None`` leaves it to the catalog inside build_order_items. Guests
        # (user_id None) and non-B2B buyers get an empty resolution → all catalog.
        # Imported locally to avoid a crud↔services import cycle at package init.
        from app.services.b2b_pricing import resolve_active_entry_map

        entry_map = resolve_active_entry_map(db, user_id=user_id)

        # Reverse charge (Phase-0 tax U4): resolved once for the whole order from
        # the buyer's confirmed company VAT-ID + the EU destination. Domestic (=
        # today's behaviour) for guests, non-B2B buyers, non-EU/home destinations,
        # or a lapsed/absent VIES confirmation. Threaded into build_order_items so
        # RC lines are priced at net with a 0% snapshot.
        tax_ctx = tax_service.resolve_tax_context(
            db, user_id=user_id, shipping_country=obj_in.shipping_country
        )

        # The stock-check + snapshot loop is shared with admin conversions
        # (quote → order today, draft orders next); duplicate-line remainder
        # semantics and the vat_rate snapshot live in build_order_items. The
        # real decrement + ledger write happens after the flush (apply_delta
        # needs order.id) — see the second loop below.
        items, subtotal, _ = build_order_items(
            db,
            line_specs=[
                OrderLineSpec(
                    product_id=line.product_id,
                    variant_id=line.variant_id,
                    quantity=line.quantity,
                    unit_price=(
                        crud_price_list.price_for(
                            entry_map,
                            product_id=line.product_id,
                            variant_id=line.variant_id,
                            currency=currency,
                        )
                        if entry_map is not None
                        else None
                    ),
                )
                for line in obj_in.items
            ],
            products=products,
            variants=variants,
            default_warehouse_id=default_warehouse.id,
            currency=currency,
            tax_ctx=tax_ctx,
        )

        # Snapshot the gift-card delivery details onto the matching lines
        # (``build_order_items`` returns items in request order). A NULL
        # recipient means "email the buyer"; mark_paid resolves that fallback at
        # issue time. Only gift-card lines carry these — a stray recipient on a
        # physical line is ignored.
        for item, line in zip(items, obj_in.items, strict=True):
            if item.is_gift_card:
                recipient = (line.gift_card_recipient_email or "").strip() or None
                message = (line.gift_card_message or "").strip() or None
                item.gift_card_recipient_email = recipient
                item.gift_card_message = message

        # Gift-card products (v2) ship nothing: buying one issues a stored-value
        # card on payment. An **all**-gift-card cart therefore skips shipping
        # entirely (no method / country / address). A mixed cart still ships the
        # physical goods normally — the gift-card lines just contribute 0 weight
        # (their weight_kg is NULL → 0 in resolve_order_shipping already).
        requires_shipping = any(not products[line.product_id].is_gift_card for line in obj_in.items)
        ship_method: ShippingMethod | None
        if requires_shipping:
            country = (obj_in.shipping_country or "").strip()
            if obj_in.shipping_method_id is None or not country:
                raise ShippingRequiredError()
            # Shipping resolution lives in the shared resolve_order_shipping helper
            # (rate lookup + InPost locker point guard + dimensional/billable
            # weight); 422s out the route on a miss. Shared with the draft-order
            # conversion so the two paths can't drift.
            shipping_amount, ship_method = resolve_order_shipping(
                db,
                product_lines=[(products[line.product_id], line.quantity) for line in obj_in.items],
                subtotal=subtotal,
                country=country,
                currency=currency,
                shipping_method_id=obj_in.shipping_method_id,
                inpost_point=obj_in.inpost_point_id,
            )
            is_locker = is_inpost_locker_method(ship_method)
        else:
            shipping_amount = ZERO_MONEY
            ship_method = None
            is_locker = False

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
        # Gift cards are excluded from discounts (Shopify parity): the coupon is
        # picked + sized against the **discountable** subtotal (goods only), so a
        # percent/fixed coupon never reduces a gift-card line and a min-order
        # threshold is measured on the goods. The order total below still uses the
        # full ``subtotal`` (gift cards included).
        gift_card_subtotal = quantize_money(
            sum((item.total_price for item in items if item.is_gift_card), ZERO_MONEY)
        )
        discountable_subtotal = quantize_money(subtotal - gift_card_subtotal)
        applied: AppliedDiscount | None = None
        excluded: set[int] = set()
        while True:
            applied = crud_coupon.pick_best(
                db,
                code=obj_in.coupon_code,
                subtotal=discountable_subtotal,
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
        # invariant ``total == quantize_money(subtotal - discount + shipping)``
        # holds and the invoice/CSV render a 0.00 shipping line, no bogus discount row).
        discount = ZERO_MONEY
        redeemed_coupon = applied.coupon if applied is not None else None
        if applied is not None:
            if applied.free_shipping:
                shipping_amount = ZERO_MONEY
            else:
                discount = applied.saving

        total = quantize_money(subtotal - discount + shipping_amount)

        # B2B net terms (Program 2): when the buyer chose "pay on account", resolve
        # their net-terms company, enforce the credit limit (hard block on this
        # option only — an over-limit buyer can still pay by card/PayU/COD), and
        # stamp the company + due date. The faktura is issued at placement (below,
        # after the flush) with the printed due date. Imported locally to avoid a
        # crud↔services import cycle at package init (mirrors b2b_pricing).
        net_terms_company_id: int | None = None
        net_terms_due_at: datetime | None = None
        if obj_in.payment_method == "net_terms":  # app.services.b2b_net_terms.NET_TERMS_METHOD
            from app.services.b2b_net_terms import (
                compute_due_date,
                convert_to_credit_currency,
                outstanding_balance,
                resolve_net_terms,
            )

            ctx = resolve_net_terms(db, user_id=user_id)
            if ctx is None:
                raise NetTermsUnavailableError()
            company = ctx.company
            if company.credit_limit is not None:
                # Credit is denominated in the company's billing currency. Convert
                # this order into that currency via admin FX rates (Program D
                # cross-currency credit); a missing rate → incomparable → reject.
                converted = convert_to_credit_currency(
                    db, company=company, amount=total, currency=currency
                )
                if converted is None:
                    raise NetTermsCurrencyMismatchError()
                outstanding = outstanding_balance(db, company=company)
                if outstanding is None:
                    # A currency in the company's AR lacks an FX rate — can't
                    # safely measure the limit, so reject rather than under-count.
                    raise NetTermsCurrencyMismatchError()
                if outstanding + converted > company.credit_limit:
                    raise CreditLimitExceededError()
            net_terms_company_id = company.id
            # The assigned payment term decides the printed due date (net_days /
            # due_on_receipt / fixed_date), not a bare Net-N offset any more.
            net_terms_due_at = compute_due_date(ctx.term, datetime.now(UTC).replace(tzinfo=None))

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
            tax=ZERO_MONEY,
            shipping=shipping_amount,
            discount=discount,
            total=total,
            currency=currency,
            coupon_id=redeemed_coupon.id if redeemed_coupon is not None else None,
            coupon_code=redeemed_coupon.code if redeemed_coupon is not None else None,
            invoice_nip=obj_in.invoice_nip,
            invoice_name=obj_in.invoice_name,
            # All-gift-card orders carry no address (the columns are NOT NULL, so
            # they store "" rather than NULL); country stays NULL like a
            # quote-converted order.
            shipping_address=obj_in.shipping_address or "",
            shipping_city=obj_in.shipping_city or "",
            shipping_postal_code=obj_in.shipping_postal_code or "",
            shipping_country=obj_in.shipping_country,
            shipping_method_id=ship_method.id if ship_method is not None else None,
            shipping_method_name=ship_method.name if ship_method is not None else None,
            inpost_point_id=(obj_in.inpost_point_id or None) if is_locker else None,
            inpost_point_name=(obj_in.inpost_point_name or None) if is_locker else None,
            # Cash-on-delivery and net-terms (pay on account) are chosen at
            # create; online methods (stripe/payu) are set later by the
            # payment-start flow (NULL until then). Net-terms orders also carry
            # the company attribution + printed due date (resolved above).
            payment_method=obj_in.payment_method,
            company_id=net_terms_company_id,
            payment_due_at=net_terms_due_at,
            # Reverse-charge treatment + the audit snapshot it rested on.
            tax_treatment=tax_ctx.treatment,
            buyer_vat_id=tax_ctx.buyer_vat_id,
            buyer_vies_confirmed_at=tax_ctx.buyer_vies_confirmed_at,
            # Tender snapshots start at 0 (not the column default) so
            # ``amount_due`` is correct pre-flush; overwritten below when a
            # gift card / store credit actually applies.
            gift_card_amount=ZERO_MONEY,
            store_credit_amount=ZERO_MONEY,
            notes=obj_in.notes,
            items=items,
        )
        db.add(order)
        db.flush()  # populate order.id before writing inventory movements

        # Open the order's timeline. actor = the placing customer (None for a
        # guest). A fully-tender-covered order emits 'paid' below (higher id →
        # sorts newer), so the reverse-chron feed reads paid → created.
        crud_order_event.record(
            db, order_id=order.id, kind="created", actor_id=user_id, payload={"status": "pending"}
        )

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

        # Stored-value tenders (D2/D10): applied AFTER the coupon against the
        # final total — gift card first (min(balance, total)), then store
        # credit (min(balance, remainder)). Debits go through the FOR-UPDATE
        # ledger seams keyed by order.id, in this same unit of work, so a
        # rolled-back order burns nothing (mirrors stock decrement + coupon
        # redeem). An unusable typed code fails LOUD (GiftCardError → 400 at
        # the route, like a bad coupon code — never silently ignored); the
        # store-credit checkbox is opportunistic (no account / zero balance
        # simply contributes 0). Toggle gating + the guest store-credit
        # rejection live at the route (409/400 with machine codes).
        gift_card_amount = ZERO_MONEY
        store_credit_amount = ZERO_MONEY
        if obj_in.gift_card_code:
            card = crud_gift_card.get_by_code(db, code=obj_in.gift_card_code)
            if card is None:
                raise GiftCardError("gift_card_not_found", "This gift card code is not valid.")
            # Fail early with the precise machine code (disabled/expired/
            # depleted/currency) before computing the tender; debit re-checks
            # everything under the row lock.
            crud_gift_card.check_usable(card=card, currency=currency)
            gift_card_amount = min(quantize_money(card.balance), total)
            if gift_card_amount > 0:
                crud_gift_card.debit(
                    db,
                    gift_card_id=card.id,
                    amount=gift_card_amount,
                    currency=currency,
                    order_id=order.id,
                )
            order.gift_card_id = card.id
            order.gift_card_last4 = card.last4
            order.gift_card_amount = gift_card_amount
        if obj_in.use_store_credit and user_id is not None:
            remainder = quantize_money(total - gift_card_amount)
            account = crud_store_credit.get_account(db, user_id=user_id, currency=currency)
            if account is not None and remainder > 0:
                store_credit_amount = min(quantize_money(account.balance), remainder)
                if store_credit_amount > 0:
                    crud_store_credit.debit(
                        db,
                        user_id=user_id,
                        currency=currency,
                        amount=store_credit_amount,
                        order_id=order.id,
                    )
                    order.store_credit_amount = store_credit_amount

        # Fully covered → PAID in this same transaction via the mark_paid
        # machinery (gap-free faktura included). ``payment_method`` per D10:
        # "gift_card" when a card contributed, else "store_credit". Providers
        # never see the order (amount_due is 0 and the payment-start pending
        # gate 409s a PAID order).
        if (gift_card_amount + store_credit_amount) > 0 and order.amount_due == 0:
            self.mark_paid(
                db,
                order=order,
                payment_intent_id=None,
                payment_method="gift_card" if gift_card_amount > 0 else "store_credit",
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

        # Net terms: the faktura IS the pay-by-invoice instrument, so it's issued
        # at placement (with the printed due date) — not on a later paid
        # transition (a net-terms order ships while unpaid and never enters PAID).
        # Gap-free sequential numbering, same unit of work as the order. A
        # tender-covered order (above) already issued via mark_paid; net-terms
        # orders never carry tenders, so there is no double-issue.
        if net_terms_company_id is not None:
            crud_invoice.issue_for_order(db, order=order)
            crud_order_event.record(
                db,
                order_id=order.id,
                kind="net_terms_placed",
                actor_id=user_id,
                payload={"due_at": net_terms_due_at.isoformat() if net_terms_due_at else None},
            )
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
        # Release stored-value tenders back (D5, symmetric to the stock
        # restore): ledger ``release`` entries via the idempotent seams — an
        # order that used neither is a no-op. The snapshot columns
        # (gift_card_amount / store_credit_amount / last4) stay for history.
        crud_gift_card.release_for_order(db, order_id=order.id)
        crud_store_credit.release_for_order(db, order_id=order.id)
        previous = OrderStatus(order.status).value
        order.status = OrderStatus.CANCELLED
        db.add(order)
        crud_order_event.record(
            db,
            order_id=order.id,
            kind="status_changed",
            actor_id=actor_user_id,
            payload={"from": previous, "to": OrderStatus.CANCELLED.value},
        )
        return order

    def is_cancellable(self, order: Order) -> bool:
        return OrderStatus(order.status) in _CANCELLABLE

    def apply_admin_update(
        self,
        db: Session,
        *,
        order: Order,
        obj_in: OrderUpdate,
        actor_user_id: int | None = None,
    ) -> Order:
        """Apply an admin PATCH (status workflow + notes/payment/tracking fields).

        Coerces ``status`` to a real ``OrderStatus`` member (the schema's
        ``use_enum_values`` would otherwise hand us a raw string that the
        SQLEnum column may reject). Tracking fields are typically set alongside
        ``status=shipped`` but may be edited on their own afterwards (a no-op
        status keeps the PATCH valid). Records timeline events (paid /
        status_changed / tracking_added) attributed to ``actor_user_id``. Does
        not flush — the audit writer reads ``get_history`` before the route
        commits (record() only db.add()s, which doesn't flush).
        """
        update_data = obj_in.model_dump(exclude_unset=True)
        previous_status = OrderStatus(order.status)
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
            if new_status != previous_status:
                # A genuine pending→paid gets the dedicated 'paid' event (the
                # route issues the faktura in the same txn); other transitions
                # are 'status_changed'.
                if new_status == OrderStatus.PAID:
                    crud_order_event.record(
                        db, order_id=order.id, kind="paid", actor_id=actor_user_id
                    )
                else:
                    crud_order_event.record(
                        db,
                        order_id=order.id,
                        kind="status_changed",
                        actor_id=actor_user_id,
                        payload={"from": previous_status.value, "to": new_status.value},
                    )
        # Tracking added/changed → a dedicated timeline entry (the shipped email
        # carries the link). Fire when a non-empty tracking_number is set to a
        # new value.
        if (
            "tracking_number" in update_data
            and update_data["tracking_number"]
            and update_data["tracking_number"] != order.tracking_number
        ):
            crud_order_event.record(
                db,
                order_id=order.id,
                kind="tracking_added",
                actor_id=actor_user_id,
                payload={
                    "tracking_number": update_data["tracking_number"],
                    "carrier": update_data.get("shipping_carrier") or order.shipping_carrier,
                },
            )
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

    def edit_items(
        self,
        db: Session,
        *,
        order: Order,
        lines: Sequence[OrderLineSpec],
        discount: Decimal | None = None,
        shipping_method_id: int | None = None,
        actor_id: int | None = None,
    ) -> Order:
        """Replace a **pending** order's line set and recompute its totals.

        Refuses any non-PENDING order (``OrderNotEditableError`` → 409 at the
        route) — a paid order has a faktura and a settled amount, so changes go
        through the refund / corrective-invoice path, not an in-place edit.

        Stock stays honest via release-then-reacquire: every current line's
        stock is restored first (so ``build_order_items``'s availability check
        sees the true level, incl. the units this order already held), the new
        line set is built + priced through the shared helpers, then the new
        lines are decremented. Both legs are ``ORDER_EDITED`` ledger rows.
        Totals go through the shared ``order_math.recompute_totals``; shipping
        is re-resolved when the order has a method (weight/method may have
        changed). Writes an ``edited`` timeline event with a before/after line
        diff. Any stored-value tender is re-fitted to the new total by
        ``_reconcile_tender`` in this same unit of work — an edit downward
        returns the excess and may settle the order outright. Caller owns the
        commit.
        """
        if OrderStatus(order.status) != OrderStatus.PENDING:
            raise OrderNotEditableError(order.status)

        currency = order.currency
        before_lines = [
            {"product_id": it.product_id, "variant_id": it.variant_id, "quantity": it.quantity}
            for it in order.items
        ]

        default_warehouse = crud_warehouse.get_default(db)
        if default_warehouse is None:
            raise RuntimeError("No default warehouse configured; run migrations")

        # 1. Release the current lines' stock so the availability re-check below
        #    accounts for the units this order already holds.
        for item in order.items:
            product = db.get(Product, item.product_id)
            if product is None:
                continue
            variant = (
                db.get(ProductVariant, item.variant_id) if item.variant_id is not None else None
            )
            crud_inventory.apply_delta(
                db,
                product=product,
                delta=item.quantity,
                reason=InventoryMovementReason.ORDER_EDITED,
                warehouse_id=default_warehouse.id,
                variant=variant,
                actor_user_id=actor_id,
                order_id=order.id,
                # A release is a pure give-back (positive delta) — it can only
                # raise the level, never make it more negative. The apply_delta
                # "cannot go negative" guard exists to stop over-decrementing, so
                # it must not fire here: an already-oversold line (level < 0 from
                # a prior backorder) would otherwise be un-releasable and the
                # whole order un-editable. The reacquire leg below stays guarded
                # (honours allow_backorder).
                allow_negative=True,
                note="order edit: released prior line",
            )
        db.flush()

        # 2. Build the new line set (stock check now correct against restored levels).
        product_ids = {line.product_id for line in lines}
        products = {
            p.id: p
            for p in db.query(Product)
            .options(selectinload(Product.variants))
            .filter(Product.id.in_(product_ids))
            .all()
        }
        variants = load_variants_for_lines(
            db, [line.variant_id for line in lines if line.variant_id is not None]
        )
        new_items, subtotal, _ = build_order_items(
            db,
            line_specs=lines,
            products=products,
            variants=variants,
            default_warehouse_id=default_warehouse.id,
            currency=currency,
            # Keep the treatment the order was placed under — re-resolving could
            # flip it if the company's VIES confirmation has since lapsed.
            tax_ctx=tax_service.TaxContext.from_order(order),
        )

        # 3. Swap the order's items (delete-orphan removes the old rows on flush).
        order.items.clear()
        for it in new_items:
            order.items.append(it)
        db.flush()

        # 4. Decrement stock for the new line set.
        for line in lines:
            crud_inventory.apply_delta(
                db,
                product=products[line.product_id],
                delta=-line.quantity,
                reason=InventoryMovementReason.ORDER_EDITED,
                warehouse_id=default_warehouse.id,
                variant=variants.get(line.variant_id) if line.variant_id is not None else None,
                actor_user_id=actor_id,
                order_id=order.id,
                allow_negative=products[line.product_id].allow_backorder,
                note="order edit: acquired new line",
            )

        # 5. Discount override (manual, like draft orders) — keep current if unset.
        if discount is not None:
            order.discount = quantize_money(discount)

        # 6. Re-resolve shipping when the order carries a method (weight or the
        #    chosen method may have changed); otherwise keep the current amount.
        method_id = (
            shipping_method_id if shipping_method_id is not None else order.shipping_method_id
        )
        if method_id is not None:
            if order.shipping_country is None:
                # A quote-converted order carries no address until fulfilment.
                # Rates are country-keyed, so re-resolving here would have to
                # invent a destination — refuse instead.
                raise OrderShippingCountryMissingError(order.id)
            shipping_amount, ship_method = resolve_order_shipping(
                db,
                product_lines=[(products[line.product_id], line.quantity) for line in lines],
                subtotal=subtotal,
                country=order.shipping_country,
                currency=currency,
                shipping_method_id=method_id,
                inpost_point=order.inpost_point_id,
            )
            order.shipping = shipping_amount
            order.shipping_method_id = ship_method.id
            order.shipping_method_name = ship_method.name

        # 7. Recompute subtotal/total via the shared seam (draft uses the same).
        order.subtotal, order.total = recompute_totals(
            line_totals=[it.total_price for it in new_items],
            discount=order.discount,
            shipping=order.shipping,
        )

        after_lines = [
            {"product_id": it.product_id, "variant_id": it.variant_id, "quantity": it.quantity}
            for it in new_items
        ]
        crud_order_event.record(
            db,
            order_id=order.id,
            kind="edited",
            actor_id=actor_id,
            payload={"before": before_lines, "after": after_lines},
        )

        # 8. Reconcile the stored-value tender against the new total, in this
        #    same unit of work (SEC-019). Recorded after the 'edited' event so
        #    the timeline reads edited → released → paid.
        self._reconcile_tender(db, order=order, actor_id=actor_id)

        db.add(order)
        return order

    def _reconcile_tender(self, db: Session, *, order: Order, actor_id: int | None = None) -> None:
        """Re-fit an edited order's gift-card / store-credit snapshots to its new total.

        ``recompute_totals`` knows nothing about tenders, so an order edited
        **downward** ends up covered by more stored value than it costs and
        ``Order.amount_due`` goes negative — breaking the invariant its own
        docstring asserts and leaving the order unsettleable (SEC-019). Edits
        *upward* need nothing: the tender still stands and the customer simply
        owes the difference.

        Unwinding is **LIFO**, store credit before gift card, the exact inverse
        of how checkout applies them (card first, credit on the remainder). That
        way the surviving split is the one checkout would itself have produced
        for the smaller total, rather than an arbitrary one.

        Each snapshot is reduced by what the ledger **actually released**, never
        by what was asked for, so a ledger that disagrees with the snapshot
        surfaces as a negative ``amount_due`` and raises
        ``OrderTenderReconciliationError`` instead of quietly claiming money
        that never moved. Caller owns the commit.
        """
        gift_amount = order.gift_card_amount or ZERO_MONEY
        credit_amount = order.store_credit_amount or ZERO_MONEY
        tendered = quantize_money(gift_amount + credit_amount)
        if tendered <= 0:
            return

        excess = quantize_money(tendered - order.total)
        if excess > 0:
            if credit_amount > 0:
                released = crud_store_credit.release_for_order(db, order_id=order.id, amount=excess)
                if released > 0:
                    order.store_credit_amount = quantize_money(credit_amount - released)
                    excess = quantize_money(excess - released)
            if excess > 0 and gift_amount > 0:
                released = crud_gift_card.release_for_order(db, order_id=order.id, amount=excess)
                if released > 0:
                    order.gift_card_amount = quantize_money(gift_amount - released)
                    excess = quantize_money(excess - released)
            db.flush()
            crud_order_event.record(
                db,
                order_id=order.id,
                kind="tender_released",
                actor_id=actor_id,
                payload={
                    "gift_card_amount": str(order.gift_card_amount or ZERO_MONEY),
                    "store_credit_amount": str(order.store_credit_amount or ZERO_MONEY),
                },
            )

        # The invariant, now enforced rather than merely asserted in a docstring.
        if order.amount_due < 0:
            raise OrderTenderReconciliationError(order.id, order.amount_due)

        # An edit downward can legitimately settle the order outright — the same
        # close-out checkout runs when stored value covers everything (D10).
        if (
            (order.gift_card_amount or ZERO_MONEY) + (order.store_credit_amount or ZERO_MONEY)
        ) > 0 and order.amount_due == 0:
            self.mark_paid(
                db,
                order=order,
                payment_intent_id=None,
                payment_method=(
                    "gift_card" if (order.gift_card_amount or ZERO_MONEY) > 0 else "store_credit"
                ),
            )

    def mark_paid(
        self,
        db: Session,
        *,
        order: Order,
        payment_intent_id: str | None,
        payment_method: str,
    ) -> Order:
        """Idempotently flip a pending order to paid.

        No-op when the order isn't pending, so duplicate/late notifications are
        safe. Records the provider's payment id + method (``"payu"`` /
        ``"stripe"``); a fully-tender-covered order (gift card / store credit)
        has no provider id — pass ``payment_intent_id=None`` and the column
        stays NULL (the tender ledgers are the payment record). Issues the
        sequential faktura number in the same transaction as the flip
        (gap-free — the number and the paid state commit or roll back together).
        Caller owns the commit.
        """
        if OrderStatus(order.status) != OrderStatus.PENDING:
            return order
        order.status = OrderStatus.PAID
        order.payment_method = payment_method
        if payment_intent_id is not None:
            order.payment_intent_id = payment_intent_id
        db.add(order)
        crud_invoice.issue_for_order(db, order=order)
        # Timeline: payment confirmed. System actor (webhook / tender close-out).
        crud_order_event.record(
            db,
            order_id=order.id,
            kind="paid",
            payload={"payment_method": payment_method},
        )
        # v2: mint the purchased gift cards now that the money is in. Runs only on
        # the PENDING→PAID edge (the early-return above makes it idempotent), and
        # the issuer re-checks provenance so a double mark_paid never double-mints.
        self._issue_purchased_gift_cards(db, order=order)
        return order

    def _issue_purchased_gift_cards(self, db: Session, *, order: Order) -> None:
        """Issue a stored-value ``GiftCard`` for every gift-card line on a paid order.

        One card per unit of quantity, value = the line's ``unit_price``, in the
        order's currency. The recipient is the line's ``gift_card_recipient_email``
        or (fallback) the buyer/guest email; the plaintext code is emailed
        best-effort (a failed send never rolls back the payment — an admin can
        re-issue). Provenance (``order_id``) doubles as the double-issue guard.
        Caller owns the commit.
        """
        gift_lines = [item for item in order.items if item.is_gift_card]
        if not gift_lines:
            return
        # Defensive idempotency: if any card already traces to this order, a prior
        # mark_paid already minted them — never mint a second set.
        if db.query(GiftCard.id).filter(GiftCard.order_id == order.id).first() is not None:
            return

        from app.services.gift_card_email import send_gift_card_email

        buyer = order.user
        buyer_email = buyer.email if buyer is not None else order.guest_email
        lang = order.guest_locale or (buyer.locale if buyer is not None else None) or "en"
        for item in gift_lines:
            recipient = item.gift_card_recipient_email or buyer_email
            for _ in range(item.quantity):
                card, code = crud_gift_card.issue(
                    db,
                    initial_value=item.unit_price,
                    currency=order.currency,
                    recipient_email=recipient,
                    user_id=order.user_id,
                    note=f"Purchased — order {order.order_number}",
                    order_id=order.id,
                )
                if recipient:
                    try:
                        send_gift_card_email(
                            email_to=recipient,
                            code=code,
                            amount=card.initial_value,
                            currency=card.currency,
                            expires_at=card.expires_at,
                            message=item.gift_card_message,
                            lang=lang,
                        )
                    except Exception:
                        logger.warning(
                            "Email send failed: kind=gift_card_purchase order_id=%s "
                            "gift_card_id=%s",
                            order.id,
                            card.id,
                            exc_info=True,
                        )

    def record_net_terms_payment(
        self, db: Session, *, order: Order, actor_user_id: int | None
    ) -> Order:
        """Record the bank transfer that settles a net-terms (pay-on-account) order.

        Stamps ``net_terms_paid_at`` (naive UTC) and appends a ``net_terms_paid``
        timeline event. Payment is tracked ORTHOGONALLY to fulfilment: the order's
        status is untouched (it may be anywhere from pending to delivered), and no
        faktura is (re-)issued — it was already issued at placement. Idempotent:
        a no-op if already paid. Caller owns the commit. Raises ``ValueError`` if
        the order isn't a net-terms order (route → 400/409).
        """
        if order.payment_method != "net_terms":
            raise ValueError("Order is not a net-terms (pay-on-account) order")
        if order.net_terms_paid_at is not None:
            return order
        paid_at = datetime.now(UTC).replace(tzinfo=None)
        order.net_terms_paid_at = paid_at
        db.add(order)
        crud_order_event.record(
            db,
            order_id=order.id,
            kind="net_terms_paid",
            actor_id=actor_user_id,
            payload={"paid_at": paid_at.isoformat()},
        )
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
