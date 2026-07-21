"""Quote CRUD — admin-managed B2B price quotes (header + line items).

Mirrors ``CRUDCompany`` for the per-resource shape and ``crud_order.create_with_items``
for the header+items create: no commit anywhere (the route owns the transaction
so the mutation and its audit row land together).

The **server computes** every ``line_total`` (= ``quantity * unit_price``) plus
the quote ``subtotal`` / ``total`` (``total == subtotal`` in MVP — no tax /
discount). The client never supplies totals. ``product_id`` on a line is an
optional, un-validated catalog link (SET NULL on product delete) — quotes can
reference free-text items that aren't catalog products, so a missing product is
not an error (mirrors the deferred, lenient posture of the MVP).

**Manage-only.** Quote→order conversion and any customer-facing flow are out of
scope and live elsewhere (when they exist).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Query, Session, selectinload

from app.core.money import ZERO_MONEY, quantize_money
from app.crud.inventory import inventory as crud_inventory
from app.crud.order import (
    OrderLineSpec,
    VariantRequiredError,
    VariantUnavailableError,
    build_order_items,
    load_variants_for_lines,
    resolve_line_variant,
)
from app.crud.order import order as crud_order
from app.crud.warehouse import warehouse as crud_warehouse
from app.models.company import Company
from app.models.inventory_movement import InventoryMovementReason
from app.models.order import Order, OrderStatus
from app.models.product import Product
from app.models.quote import Quote, QuoteItem, QuoteStatus
from app.schemas.quote import QuoteCreate, QuoteItemCreate, QuoteRequestCreate, QuoteUpdate


class QuoteRequestError(Exception):
    """A customer quote request fails a precondition.

    Carries a machine ``reason`` code + the HTTP ``status_code`` the customer
    route maps to the response (422 for a bad/inactive product line). Mirrors
    ``QuoteNotConvertibleError`` so the route stays a thin mapper.
    """

    def __init__(self, *, reason: str, message: str, status_code: int = 422) -> None:
        self.reason = reason
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class QuoteNotAcceptableError(Exception):
    """An accept-transition precondition fails (source state is not ``sent``).

    Maps to 409 in the customer route. Mirrors the convert/redeem error posture.
    """

    def __init__(self, *, message: str = "only a sent quote can be accepted") -> None:
        self.message = message
        super().__init__(message)


class QuoteNotConvertibleError(Exception):
    """A quote fails a convert eligibility precondition.

    Carries a machine ``reason`` code + an HTTP ``status_code`` the route maps to
    the response (422 for state/data preconditions, 409 for already-converted).
    The convert route raises the matching ``HTTPException`` from this.
    """

    def __init__(self, *, reason: str, message: str, status_code: int) -> None:
        self.reason = reason
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _build_items(lines: list[QuoteItemCreate]) -> tuple[list[QuoteItem], Decimal]:
    """Build ``QuoteItem`` rows with computed ``line_total`` + return the subtotal.

    ``line_total = quantize_money(quantity * unit_price)``; the subtotal is the
    quantized sum of the line totals. Inbound ``unit_price`` is a wire float, so
    it's converted to a money ``Decimal`` here. Pure — no DB access
    (``product_id`` is stored as-is, not validated).
    """
    items: list[QuoteItem] = []
    subtotal = ZERO_MONEY
    for line in lines:
        unit_price = quantize_money(line.unit_price)
        line_total = quantize_money(line.quantity * unit_price)
        subtotal += line_total
        items.append(
            QuoteItem(
                product_id=line.product_id,
                variant_id=line.variant_id,
                description=line.description,
                quantity=line.quantity,
                unit_price=unit_price,
                line_total=line_total,
            )
        )
    return items, quantize_money(subtotal)


class CRUDQuote:
    """Quote rows + nested items (no commit; caller owns the txn)."""

    def get(self, db: Session, id: int) -> Quote | None:
        """Fetch one quote with its company + items eager-loaded (no N+1)."""
        return (
            db.query(Quote)
            .options(selectinload(Quote.company), selectinload(Quote.items))
            .filter(Quote.id == id)
            .first()
        )

    def company_exists(self, db: Session, company_id: int) -> bool:
        """True if a company with ``company_id`` exists (route 404s on False)."""
        return db.query(Company.id).filter(Company.id == company_id).first() is not None

    def get_multi(
        self,
        db: Session,
        *,
        status: str | None = None,
        company_id: int | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[Quote]:
        query = self._filtered(db, status=status, company_id=company_id)
        return (
            query.options(selectinload(Quote.company), selectinload(Quote.items))
            .order_by(Quote.created_at.desc(), Quote.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count(
        self, db: Session, *, status: str | None = None, company_id: int | None = None
    ) -> int:
        return self._filtered(db, status=status, company_id=company_id).count()

    def _filtered(self, db: Session, *, status: str | None, company_id: int | None) -> Query[Quote]:
        query = db.query(Quote)
        if status is not None:
            query = query.filter(Quote.status == status)
        if company_id is not None:
            query = query.filter(Quote.company_id == company_id)
        return query

    def create_with_items(self, db: Session, *, obj_in: QuoteCreate) -> Quote:
        """Create a quote from posted line items.

        Computes each ``line_total`` + the ``subtotal`` / ``total`` server-side.
        Flushes so the ``id`` is populated; does not commit. The caller checks
        the company exists first (``company_exists``) and 404s on miss.
        """
        items, subtotal = _build_items(obj_in.items)
        quote = Quote(
            company_id=obj_in.company_id,
            status="draft",
            currency=obj_in.currency,
            valid_until=obj_in.valid_until,
            notes=obj_in.notes,
            subtotal=subtotal,
            total=subtotal,  # total == subtotal in MVP
            items=items,
        )
        db.add(quote)
        db.flush()
        db.refresh(quote)
        return quote

    # ------------------------------------------------------------------
    # Customer-facing (Wave C / B2B-1) — company-scoped reads + request + accept.
    # ------------------------------------------------------------------

    def list_for_company(self, db: Session, *, company_id: int) -> list[Quote]:
        """All quotes for ``company_id``, newest-first, items eager-loaded."""
        return (
            db.query(Quote)
            .options(selectinload(Quote.company), selectinload(Quote.items))
            .filter(Quote.company_id == company_id)
            .order_by(Quote.created_at.desc(), Quote.id.desc())
            .all()
        )

    def get_for_company(self, db: Session, *, quote_id: int, company_id: int) -> Quote | None:
        """Fetch one quote scoped to ``company_id`` (cross-company → ``None`` → 404)."""
        return (
            db.query(Quote)
            .options(selectinload(Quote.company), selectinload(Quote.items))
            .filter(Quote.id == quote_id, Quote.company_id == company_id)
            .first()
        )

    def request_from_cart(
        self,
        db: Session,
        *,
        obj_in: QuoteRequestCreate,
        company_id: int,
        requested_by_user_id: int,
    ) -> Quote:
        """Create a ``requested`` quote from a customer's cart (no commit; caller owns txn).

        **Server-priced (D-C-4 — mirrors the coupon discipline):** each line's
        ``unit_price`` comes from the CURRENT product price in ``obj_in.currency``
        via ``Product.get_price`` — a client never supplies a price. Each
        ``product_id`` must reference an existing, active product (else
        ``QuoteRequestError`` → 422); the line ``description`` is snapshotted from
        the live product name. Variant lines follow the order-create rules
        (``resolve_line_variant``): a product with active variants requires a
        ``variant_id`` (→ 422 ``variant_required``), the variant must be this
        product's and active (→ 422 ``variant_unavailable``); pricing comes from
        ``variant.get_price`` (NULL inherits the product) and the description
        gets the variant title appended (``"Widget — Red / XL"``). Reuses
        ``_build_items`` after server pricing so the ``line_total`` /
        ``subtotal`` math is shared with the admin path.

        Sets ``status='requested'``, ``company_id`` (the caller's company), and
        ``requested_by_user_id`` (the caller). Flushes so the ``id`` is populated.
        """
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

        priced_lines: list[QuoteItemCreate] = []
        for line in obj_in.items:
            product = products.get(line.product_id)
            if product is None or not product.is_active:
                raise QuoteRequestError(
                    reason="product_unavailable",
                    message=f"Product {line.product_id} is unavailable",
                )
            # Re-raise the shared variant rules as QuoteRequestError so the
            # customer route stays a thin status_code mapper.
            try:
                variant = resolve_line_variant(product, line.variant_id, variants)
            except VariantRequiredError as exc:
                raise QuoteRequestError(reason="variant_required", message=str(exc)) from exc
            except VariantUnavailableError as exc:
                raise QuoteRequestError(reason="variant_unavailable", message=str(exc)) from exc
            if variant is not None:
                unit_price = variant.get_price(obj_in.currency)
                description = f"{product.name} — {variant.title}"
            else:
                unit_price = product.get_price(obj_in.currency)
                description = product.name
            priced_lines.append(
                QuoteItemCreate(
                    description=description,
                    # QuoteItemCreate is the wire schema (float); _build_items
                    # re-quantizes to a money Decimal.
                    unit_price=float(unit_price),
                    quantity=line.quantity,
                    product_id=product.id,
                    variant_id=variant.id if variant is not None else None,
                )
            )

        items, subtotal = _build_items(priced_lines)
        quote = Quote(
            company_id=company_id,
            status=QuoteStatus.REQUESTED.value,
            currency=obj_in.currency,
            valid_until=None,
            notes=obj_in.notes,
            subtotal=subtotal,
            total=subtotal,  # total == subtotal in MVP
            requested_by_user_id=requested_by_user_id,
            items=items,
        )
        db.add(quote)
        db.flush()
        db.refresh(quote)
        return quote

    def accept(self, db: Session, *, quote: Quote) -> Quote:
        """Customer-accept a ``sent`` quote → ``accepted`` (no flush; caller commits).

        Only a ``sent`` quote may be accepted (D-C-5); any other source state
        raises ``QuoteNotAcceptableError`` (→ 409). Conversion stays admin-only.
        """
        if quote.status != QuoteStatus.SENT.value:
            raise QuoteNotAcceptableError()
        quote.status = QuoteStatus.ACCEPTED.value
        db.add(quote)
        return quote

    def update(self, db: Session, *, quote: Quote, obj_in: QuoteUpdate) -> Quote:
        """Apply a partial update (no flush — the audit writer reads ``get_history``
        between this and the caller's commit).

        Scalar fields (``status`` / ``currency`` / ``valid_until`` / ``notes``)
        are set only when explicitly sent. If ``items`` is provided, the whole
        line set is replaced (``delete-orphan`` removes the old rows) and the
        ``subtotal`` / ``total`` are recomputed; if ``items`` is omitted the lines
        and totals are left untouched.
        """
        data = obj_in.model_dump(exclude_unset=True)
        for field in ("status", "currency", "valid_until", "notes"):
            if field in data:
                setattr(quote, field, data[field])
        if obj_in.items is not None:
            items, subtotal = _build_items(obj_in.items)
            quote.items = items
            quote.subtotal = subtotal
            quote.total = subtotal  # total == subtotal in MVP
        db.add(quote)
        return quote

    def convert_to_order(self, db: Session, *, quote: Quote, actor_user_id: int) -> Order:
        """Convert an accepted quote into a pending order (no commit; caller owns the txn).

        Attribution is to a **real customer** — the quote's company primary
        contact (``company.primary_user_id``) — not the converting admin
        (``actor_user_id`` is only the audit actor, threaded into the inventory
        movements). Honours the quoted ``unit_price``/``quantity`` per line (does
        NOT re-price), snapshots ``product_name``/``product_sku`` from the live
        ``Product``, and decrements stock from the default warehouse exactly like
        ``crud_order.create_with_items`` (one ORDER_PLACED movement per line).

        Eligibility is enforced here (raises ``QuoteNotConvertibleError`` with a
        machine reason + status code) so the route is a thin mapper:
        - ``status != 'accepted'`` → 422 ``not_accepted``
        - ``converted_order_id`` already set → 409 ``already_converted``
        - no items → 422 ``no_items``
        - any line with ``product_id IS NULL`` → 422 ``free_text_line``
        - company ``primary_user_id IS NULL`` → 422 ``no_primary_contact``
        - product missing/inactive → 422 ``product_unavailable``
        - variant missing/inactive/another product's, or the product has
          active variants but the line carries no ``variant_id`` (a
          since-deleted variant's SET NULL) → 422 ``variant_unavailable``
        - insufficient stock (no backorder) → 422 ``insufficient_stock``

        Sets ``shipping=0`` / ``shipping_method_id=NULL`` (quotes carry no shipping
        context — admin sets it later), ``discount=0`` / ``tax=0``;
        ``subtotal``/``total`` come from the quote. After building the order it
        sets ``quote.converted_order_id`` (the conversion marker; ``quote.status``
        stays ``accepted``).
        """
        if quote.status != QuoteStatus.ACCEPTED.value:
            raise QuoteNotConvertibleError(
                reason="not_accepted",
                message="only accepted quotes can be converted",
                status_code=422,
            )
        if quote.converted_order_id is not None:
            raise QuoteNotConvertibleError(
                reason="already_converted",
                message="quote already converted",
                status_code=409,
            )
        if not quote.items:
            raise QuoteNotConvertibleError(
                reason="no_items",
                message="cannot convert a quote with no line items",
                status_code=422,
            )
        if any(item.product_id is None for item in quote.items):
            raise QuoteNotConvertibleError(
                reason="free_text_line",
                message="all quote lines must reference a product to convert",
                status_code=422,
            )
        if quote.company.primary_user_id is None:
            raise QuoteNotConvertibleError(
                reason="no_primary_contact",
                message="set a primary contact on the company before converting",
                status_code=422,
            )

        default_warehouse = crud_warehouse.get_default(db)
        if default_warehouse is None:
            raise RuntimeError("No default warehouse configured; run migrations")

        # Resolve products in one query; validate availability against the default
        # warehouse with a local running remainder (duplicate lines decrement
        # cumulatively) — mirrors crud_order.create_with_items' first loop.
        product_ids = [item.product_id for item in quote.items]
        products = {
            p.id: p
            for p in db.query(Product)
            .options(selectinload(Product.variants))
            .filter(Product.id.in_(product_ids))
            .all()
        }
        variants = load_variants_for_lines(
            db, [line.variant_id for line in quote.items if line.variant_id is not None]
        )
        # Shared stock-check + snapshot loop (crud_order.build_order_items):
        # honours the quoted unit_price (does NOT re-price) and snapshots
        # vat_rate from the product so converted B2B orders faktura-decompose
        # correctly for reduced-rate goods. Variant eligibility — a stale line
        # (variant deleted → SET NULL on a product that still has active
        # variants) or a missing/inactive/foreign variant must fail with a
        # clear reason rather than silently converting at product grain
        # (plan §7 risk 9).
        line_specs = []
        for line in quote.items:
            # product_id is guaranteed non-None by the free_text_line guard above.
            assert line.product_id is not None  # noqa: S101 -- guarded by the free_text_line check above
            line_specs.append(
                OrderLineSpec(
                    product_id=line.product_id,
                    variant_id=line.variant_id,
                    quantity=line.quantity,
                    unit_price=line.unit_price,  # honour the quoted price — do NOT re-price
                    line_key=line.id,
                )
            )
        try:
            order_items, _, line_variants = build_order_items(
                db,
                line_specs=line_specs,
                products=products,
                variants=variants,
                default_warehouse_id=default_warehouse.id,
                currency=quote.currency,
            )
        except (VariantRequiredError, VariantUnavailableError) as exc:
            raise QuoteNotConvertibleError(
                reason="variant_unavailable",
                message=str(exc),
                status_code=422,
            ) from exc

        company_name = quote.company.name
        order = Order(
            user_id=quote.company.primary_user_id,
            order_number=crud_order._generate_order_number(db),
            status=OrderStatus.PENDING,
            subtotal=quote.subtotal,
            tax=0.0,
            shipping=0.0,
            discount=0.0,
            total=quote.total,
            currency=quote.currency,
            # No shipping context on a quote — the admin sets the address + method
            # at fulfilment. The remaining address columns are NOT NULL, so they
            # seed a clear "to be set" placeholder rather than an empty string;
            # ``shipping_country`` is ISO-2-constrained and cannot hold a
            # placeholder, so it stays NULL — which is the honest value here.
            shipping_address="TBD — set at fulfilment",
            shipping_city="TBD",
            shipping_postal_code="TBD",
            shipping_country=None,
            shipping_method_id=None,
            notes=f"Converted from quote #{quote.id} — {company_name}",
            items=order_items,
        )
        db.add(order)
        db.flush()  # populate order.id before writing inventory movements

        for line in quote.items:
            assert line.product_id is not None  # noqa: S101 -- guarded by the free_text_line check above
            product = products[line.product_id]
            crud_inventory.apply_delta(
                db,
                product=product,
                delta=-line.quantity,
                reason=InventoryMovementReason.ORDER_PLACED,
                warehouse_id=default_warehouse.id,
                variant=line_variants.get(line.id),
                actor_user_id=actor_user_id,
                order_id=order.id,
                allow_negative=product.allow_backorder,
            )

        # Set the conversion marker but do NOT flush afterwards: the route's
        # ``record_update(resource_type="quote")`` reads ``get_history`` (cleared by
        # flush) to diff the change, and the route owns the commit. ``order`` is
        # already flushed (above) so its id + items are populated for the response.
        quote.converted_order_id = order.id
        db.add(quote)
        return order

    def delete(self, db: Session, *, quote: Quote) -> None:
        """Delete a quote; its items cascade (FK ON DELETE CASCADE + delete-orphan)."""
        db.delete(quote)


quote = CRUDQuote()
