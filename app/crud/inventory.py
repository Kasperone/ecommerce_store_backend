"""Inventory CRUD: the single choke-point for every change to ``Product.stock``.

``apply_delta`` mutates stock **and** appends an ``inventory_movements`` ledger
row in one step, without committing — the caller owns the transaction (same
contract as ``AuditWriter`` and ``CRUDOrder``). Every stock change in the app
flows through it: the manual admin adjust and the retrofitted order place/cancel
paths. The read helpers power the ``/admin/inventory`` list + movement log.
"""

from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.inventory_movement import InventoryMovement, InventoryMovementReason
from app.models.product import Product
from app.schemas.inventory import InventoryStatus


class CRUDInventory:
    """Stock-mutation choke-point + admin read helpers (no commit; caller owns the txn)."""

    def apply_delta(
        self,
        db: Session,
        *,
        product: Product,
        delta: int,
        reason: InventoryMovementReason,
        actor_user_id: int | None = None,
        order_id: int | None = None,
        note: str | None = None,
    ) -> InventoryMovement:
        """Apply ``delta`` to ``product.stock`` and append a ledger row.

        Does **not** commit or flush — the caller's transaction owns the boundary
        (so the order/audit row and the stock change land or roll back together).
        Sets ``product_id`` directly (not the ``product`` relationship) so building
        the row never triggers an autoflush that would clear ``get_history`` for
        an audit writer reading the diff later in the same request.

        Raises ``ValueError`` if the result would be negative — a defense-in-depth
        guard; real callers validate first and surface a proper 4xx (the order path
        checks availability; the manual-adjust route rejects ``new_stock < 0``).
        """
        new_stock = product.stock + delta
        if new_stock < 0:
            raise ValueError(
                f"Stock for product {product.id} cannot go negative "
                f"(current {product.stock}, delta {delta:+d})"
            )
        product.stock = new_stock
        movement = InventoryMovement(
            product_id=product.id,
            delta=delta,
            resulting_stock=new_stock,
            reason=reason.value,
            actor_user_id=actor_user_id,
            order_id=order_id,
            note=note,
        )
        db.add(movement)
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
