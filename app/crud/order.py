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
from datetime import UTC, datetime

from sqlalchemy.orm import Session, selectinload

from app.crud.base import CRUDBase
from app.crud.inventory import inventory as crud_inventory
from app.models.inventory_movement import InventoryMovementReason
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.user import User, UserRole
from app.schemas.order import OrderCreate, OrderUpdate

# Forward-only status workflow for the admin PATCH endpoint. Terminal and
# stock-affecting transitions (cancelled, refunded) are NOT in this map —
# cancellation goes through the dedicated cancel endpoint so stock is restored,
# and refunds are Phase B. Keyed by OrderStatus; str-enum members hash equal to
# their values so lookups work whether the key arrives as a member or a string.
_STATUS_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {OrderStatus.PAID, OrderStatus.PROCESSING},
    OrderStatus.PAID: {OrderStatus.PROCESSING, OrderStatus.SHIPPED},
    OrderStatus.PROCESSING: {OrderStatus.SHIPPED},
    OrderStatus.SHIPPED: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REFUNDED: set(),
}

# Statuses from which an order may still be cancelled (stock-restoring).
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


def status_transition_error(current: OrderStatus, target: OrderStatus) -> str | None:
    """Return an error message if ``current → target`` is not allowed, else None.

    A no-op transition (``current == target``) is allowed so an admin can edit
    notes/payment fields without changing status.
    """
    current = OrderStatus(current)
    target = OrderStatus(target)
    if current == target:
        return None
    if target in (OrderStatus.CANCELLED, OrderStatus.REFUNDED):
        return "Use the cancel endpoint to cancel an order; refunds are not yet supported"
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

    def create_with_items(self, db: Session, *, user_id: int, obj_in: OrderCreate) -> Order:
        """Create an order from posted line items.

        Validates availability + stock, snapshots product fields, decrements
        stock, and computes pricing. Raises ``ProductUnavailableError`` /
        ``InsufficientStockError`` (the route maps these to 422). Flushes so the
        ``id`` is populated; does not commit.
        """
        currency = obj_in.currency
        product_ids = [line.product_id for line in obj_in.items]
        products = {p.id: p for p in db.query(Product).filter(Product.id.in_(product_ids)).all()}

        items: list[OrderItem] = []
        subtotal = 0.0
        # Validate against a local running remainder so duplicate lines for the
        # same product are checked against the not-yet-committed total. The real
        # decrement + ledger write happens after the flush (apply_delta needs
        # order.id) — see the second loop below.
        remaining: dict[int, int] = {}
        for line in obj_in.items:
            product = products.get(line.product_id)
            if product is None or not product.is_active:
                raise ProductUnavailableError(line.product_id)
            available = remaining.get(product.id, product.stock)
            if available < line.quantity:
                raise InsufficientStockError(product.id, product.name, line.quantity, available)
            remaining[product.id] = available - line.quantity
            unit_price = product.get_price(currency)
            line_total = round(unit_price * line.quantity, 2)
            subtotal += line_total
            items.append(
                OrderItem(
                    product_id=product.id,
                    product_name=product.name,
                    product_sku=product.sku,
                    quantity=line.quantity,
                    unit_price=unit_price,
                    total_price=line_total,
                )
            )

        subtotal = round(subtotal, 2)
        order = Order(
            user_id=user_id,
            order_number=self._generate_order_number(db),
            status=OrderStatus.PENDING,
            subtotal=subtotal,
            tax=0.0,
            shipping=0.0,
            total=subtotal,
            currency=currency,
            shipping_address=obj_in.shipping_address,
            shipping_city=obj_in.shipping_city,
            shipping_postal_code=obj_in.shipping_postal_code,
            shipping_country=obj_in.shipping_country,
            notes=obj_in.notes,
            items=items,
        )
        db.add(order)
        db.flush()  # populate order.id before writing inventory movements

        # Decrement stock + append the ledger now that order.id exists; one
        # ORDER_PLACED movement per line (duplicate lines decrement cumulatively).
        for line in obj_in.items:
            crud_inventory.apply_delta(
                db,
                product=products[line.product_id],
                delta=-line.quantity,
                reason=InventoryMovementReason.ORDER_PLACED,
                actor_user_id=user_id,
                order_id=order.id,
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
        Caller is responsible for the cancellable-status check and the commit.
        """
        for item in order.items:
            product = db.get(Product, item.product_id)
            if product is not None:
                crud_inventory.apply_delta(
                    db,
                    product=product,
                    delta=item.quantity,
                    reason=InventoryMovementReason.ORDER_CANCELLED,
                    actor_user_id=actor_user_id,
                    order_id=order.id,
                )
        order.status = OrderStatus.CANCELLED
        db.add(order)
        return order

    def is_cancellable(self, order: Order) -> bool:
        return OrderStatus(order.status) in _CANCELLABLE

    def apply_admin_update(self, db: Session, *, order: Order, obj_in: OrderUpdate) -> Order:
        """Apply an admin PATCH (status workflow + notes/payment fields).

        Coerces ``status`` to a real ``OrderStatus`` member (the schema's
        ``use_enum_values`` would otherwise hand us a raw string that the
        SQLEnum column may reject). Does not flush — the audit writer reads
        ``get_history`` before the route commits.
        """
        update_data = obj_in.model_dump(exclude_unset=True)
        if "status" in update_data and obj_in.status is not None:
            order.status = OrderStatus(obj_in.status)
        for field in ("payment_method", "payment_intent_id", "notes"):
            if field in update_data:
                setattr(order, field, update_data[field])
        db.add(order)
        return order

    def get_scoped(self, db: Session, *, order_id: int, user: User) -> Order | None:
        """Fetch one order visible to ``user``.

        Customers see only their own; admins see any. Returns None for both
        not-found and not-owned so the route can 404 without leaking existence.
        """
        query = (
            db.query(Order)
            .options(selectinload(Order.items), selectinload(Order.user))
            .filter(Order.id == order_id)
        )
        if user.role != UserRole.ADMIN:
            query = query.filter(Order.user_id == user.id)
        return query.first()

    def get_multi_scoped(
        self,
        db: Session,
        *,
        user: User,
        status: OrderStatus | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> list[Order]:
        query = db.query(Order).options(selectinload(Order.items), selectinload(Order.user))
        if user.role != UserRole.ADMIN:
            query = query.filter(Order.user_id == user.id)
        if status is not None:
            query = query.filter(Order.status == status)
        return (
            query.order_by(Order.created_at.desc(), Order.id.desc()).offset(skip).limit(limit).all()
        )

    def count_scoped(self, db: Session, *, user: User, status: OrderStatus | None = None) -> int:
        query = db.query(Order)
        if user.role != UserRole.ADMIN:
            query = query.filter(Order.user_id == user.id)
        if status is not None:
            query = query.filter(Order.status == status)
        return query.count()


order = CRUDOrder(Order)
