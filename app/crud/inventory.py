"""Inventory CRUD: the single choke-point for every change to stock.

``apply_delta`` mutates a per-warehouse ``StockLevel`` **and** the denormalized
``Product.stock`` total (the sum across levels) in lock-step, appends an
``inventory_movements`` ledger row, and does not commit — the caller owns the
transaction (same contract as ``AuditWriter`` and ``CRUDOrder``). Every stock
change in the app flows through it: the manual admin adjust, restock, transfer,
and the order place/cancel paths. Because it is the only writer, the
``Product.stock == sum(StockLevel.quantity)`` invariant holds without a trigger
(the same denormalized-aggregate-through-a-choke-point pattern as
``Review.helpful_count``). The read helpers power the ``/admin/inventory`` list +
movement log.
"""

from __future__ import annotations

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.crud.warehouse import warehouse as crud_warehouse
from app.models.inventory_movement import InventoryMovement, InventoryMovementReason
from app.models.product import Product
from app.models.warehouse import StockLevel
from app.schemas.inventory import InventoryStatus


class CRUDInventory:
    """Stock-mutation choke-point + admin read helpers (no commit; caller owns the txn)."""

    def _get_or_create_level(
        self, db: Session, *, product: Product, warehouse_id: int, is_default: bool
    ) -> StockLevel:
        """Return the (product, warehouse) stock level, creating it if absent.

        A new **default**-warehouse level is seeded from the product's
        *unallocated* stock — ``product.stock`` minus what already sits in other
        warehouses — so a product that has stock but no materialised level (one
        created before this table existed, or via a test factory) reconciles to
        its total. Non-default levels seed at 0. The new row is flushed so a later
        ``apply_delta`` in the same unit of work (e.g. a multi-line order on the
        same product) finds it instead of inserting a duplicate.

        Called **before** the caller mutates ``product.stock``, so this flush
        can't clear a pending ``product.stock`` change from ``get_history`` (the
        manual-adjust route's audit reads that diff after ``apply_delta`` returns).
        """
        level = (
            db.query(StockLevel)
            .filter(
                StockLevel.product_id == product.id,
                StockLevel.warehouse_id == warehouse_id,
            )
            .first()
        )
        if level is not None:
            return level
        if is_default:
            other = (
                db.query(func.coalesce(func.sum(StockLevel.quantity), 0))
                .filter(
                    StockLevel.product_id == product.id,
                    StockLevel.warehouse_id != warehouse_id,
                )
                .scalar()
            )
            seed = product.stock - (other or 0)
        else:
            seed = 0
        level = StockLevel(product_id=product.id, warehouse_id=warehouse_id, quantity=seed)
        db.add(level)
        db.flush()
        return level

    def apply_delta(
        self,
        db: Session,
        *,
        product: Product,
        delta: int,
        reason: InventoryMovementReason,
        warehouse_id: int | None = None,
        actor_user_id: int | None = None,
        order_id: int | None = None,
        note: str | None = None,
        allow_negative: bool = False,
    ) -> InventoryMovement:
        """Apply ``delta`` to a warehouse's stock level and append a ledger row.

        ``warehouse_id`` defaults to the default warehouse (orders fulfil from it,
        and the manual-adjust route relied on a single location). Mutates the
        per-warehouse ``StockLevel`` **and** the denormalized ``product.stock``
        total in lock-step, then writes the movement (``resulting_stock`` is the
        warehouse's level after the delta). Does **not** commit or flush past the
        get-or-create — the caller's transaction owns the boundary so the
        order/audit row and the stock change land or roll back together.

        Raises ``ValueError`` if the warehouse level would go negative, unless
        ``allow_negative`` (the backorder path). Real callers validate first and
        surface a proper 4xx; this is the defense-in-depth guard.
        """
        default = crud_warehouse.get_default(db)
        if default is None:
            raise RuntimeError("No default warehouse configured; run migrations")
        target_id = warehouse_id if warehouse_id is not None else default.id

        level = self._get_or_create_level(
            db, product=product, warehouse_id=target_id, is_default=(target_id == default.id)
        )
        new_level = level.quantity + delta
        if new_level < 0 and not allow_negative:
            raise ValueError(
                f"Stock for product {product.id} in warehouse {target_id} cannot go "
                f"negative (current {level.quantity}, delta {delta:+d})"
            )
        level.quantity = new_level
        product.stock = product.stock + delta
        movement = InventoryMovement(
            product_id=product.id,
            warehouse_id=target_id,
            delta=delta,
            resulting_stock=new_level,
            reason=reason.value,
            actor_user_id=actor_user_id,
            order_id=order_id,
            note=note,
        )
        db.add(movement)
        db.add(level)
        db.add(product)
        return movement

    def status_for(self, product: Product) -> InventoryStatus:
        """Stock status: ``out_of_stock`` (<=0) / ``low_stock`` (at or below a set
        threshold, but >0) / ``in_stock``. A null threshold means never "low"."""
        if product.stock <= 0:
            return "out_of_stock"
        if product.low_stock_threshold is not None and product.stock <= product.low_stock_threshold:
            return "low_stock"
        return "in_stock"

    # ------------------------------------------------------------------
    # Admin stock list (products joined with computed status)
    # ------------------------------------------------------------------

    def _admin_filtered_query(self, db: Session, *, status: str | None, search: str | None):
        query = db.query(Product)
        if search:
            like = f"%{search.strip()}%"
            query = query.filter(or_(Product.name.ilike(like), Product.sku.ilike(like)))
        if status == "out_of_stock":
            query = query.filter(Product.stock <= 0)
        elif status == "low_stock":
            query = query.filter(
                Product.low_stock_threshold.isnot(None),
                Product.stock > 0,
                Product.stock <= Product.low_stock_threshold,
            )
        elif status == "in_stock":
            query = query.filter(
                Product.stock > 0,
                or_(
                    Product.low_stock_threshold.is_(None),
                    Product.stock > Product.low_stock_threshold,
                ),
            )
        return query

    def get_multi_admin(
        self,
        db: Session,
        *,
        status: str | None = None,
        search: str | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> list[Product]:
        """Admin stock list with optional status + search filters, by name."""
        return (
            self._admin_filtered_query(db, status=status, search=search)
            .order_by(Product.name.asc(), Product.id.asc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count_admin(
        self, db: Session, *, status: str | None = None, search: str | None = None
    ) -> int:
        return self._admin_filtered_query(db, status=status, search=search).count()

    # ------------------------------------------------------------------
    # Movement log
    # ------------------------------------------------------------------

    def movements_for(
        self,
        db: Session,
        *,
        product_id: int | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> list[tuple[InventoryMovement, str]]:
        """Movement log rows as ``(movement, product_name)``, newest first.

        Joins ``Product`` so the list view shows the product name without an N+1.
        """
        query = db.query(InventoryMovement, Product.name).join(
            Product, InventoryMovement.product_id == Product.id
        )
        if product_id is not None:
            query = query.filter(InventoryMovement.product_id == product_id)
        rows = (
            query.order_by(InventoryMovement.created_at.desc(), InventoryMovement.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )
        return [(movement, name) for movement, name in rows]

    def count_movements(self, db: Session, *, product_id: int | None = None) -> int:
        query = db.query(InventoryMovement)
        if product_id is not None:
            query = query.filter(InventoryMovement.product_id == product_id)
        return query.count()


inventory = CRUDInventory()
