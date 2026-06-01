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

from sqlalchemy.orm import Query, Session, selectinload

from app.crud.inventory import inventory as crud_inventory
from app.crud.order import InsufficientStockError, ProductUnavailableError
from app.crud.order import order as crud_order
from app.crud.warehouse import warehouse as crud_warehouse
from app.models.company import Company
from app.models.inventory_movement import InventoryMovementReason
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.quote import Quote, QuoteItem, QuoteStatus
from app.schemas.quote import QuoteCreate, QuoteItemCreate, QuoteUpdate


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


def _build_items(lines: list[QuoteItemCreate]) -> tuple[list[QuoteItem], float]:
    """Build ``QuoteItem`` rows with computed ``line_total`` + return the subtotal.

    ``line_total = round(quantity * unit_price, 2)``; the subtotal is the rounded
    sum of the line totals. Pure — no DB access (``product_id`` is stored as-is,
    not validated).
    """
    items: list[QuoteItem] = []
    subtotal = 0.0
    for line in lines:
        line_total = round(line.quantity * line.unit_price, 2)
        subtotal += line_total
        items.append(
            QuoteItem(
                product_id=line.product_id,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                line_total=line_total,
            )
        )
    return items, round(subtotal, 2)


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
        products = {p.id: p for p in db.query(Product).filter(Product.id.in_(product_ids)).all()}
        remaining: dict[int, int] = {}
        order_items: list[OrderItem] = []
        for line in quote.items:
            # product_id is guaranteed non-None by the free_text_line guard above;
            # bind a non-Optional local so the lookups/error are typed cleanly.
            assert line.product_id is not None  # noqa: S101 -- guarded by the free_text_line check above
            product_id = line.product_id
            product = products.get(product_id)
            if product is None or not product.is_active:
                raise ProductUnavailableError(product_id)
            available = remaining.get(
                product.id,
                crud_inventory.get_level_quantity(
                    db, product=product, warehouse_id=default_warehouse.id
                ),
            )
            if available < line.quantity and not product.allow_backorder:
                raise InsufficientStockError(product.id, product.name, line.quantity, available)
            remaining[product.id] = available - line.quantity
            order_items.append(
                OrderItem(
                    product_id=product.id,
                    product_name=product.name,
                    product_sku=product.sku,
                    quantity=line.quantity,
                    unit_price=line.unit_price,  # honour the quoted price — do NOT re-price
                    total_price=round(line.unit_price * line.quantity, 2),
                )
            )

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
            # at fulfilment. The shipping_address columns are NOT NULL and the
            # OrderResponse requires non-empty values, so seed a clear "to be set"
            # placeholder rather than an empty string.
            shipping_address="TBD — set at fulfilment",
            shipping_city="TBD",
            shipping_postal_code="TBD",
            shipping_country="TBD",
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
