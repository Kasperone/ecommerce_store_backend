"""Warehouse CRUD — stock locations + the single default fulfilment warehouse.

Stock quantities live on ``StockLevel`` rows (managed by ``crud_inventory``);
this module owns the warehouse rows. ``set_default`` reassigns the default in one
transaction: it demotes the current default(s) under ``no_autoflush`` so the
target's pending ``is_default=True`` is never flushed while another ``True`` row
still exists (which the ``uq_warehouses_default`` partial unique index would
reject). No commit — the caller owns the transaction. Mirrors
``CRUDProductImage.set_primary``.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.warehouse import StockLevel, Warehouse
from app.schemas.warehouse import WarehouseCreate, WarehouseUpdate


class CRUDWarehouse:
    """Warehouse rows + default-warehouse management (no commit; caller owns the txn)."""

    def get(self, db: Session, id: int) -> Warehouse | None:
        return db.get(Warehouse, id)

    def get_by_code(self, db: Session, *, code: str) -> Warehouse | None:
        return db.query(Warehouse).filter(Warehouse.code == code).first()

    def get_default(self, db: Session) -> Warehouse | None:
        """The single default warehouse (orders fulfil from it). The migration
        seeds exactly one; the partial unique index guarantees at most one."""
        return db.query(Warehouse).filter(Warehouse.is_default.is_(True)).first()

    def get_multi(
        self, db: Session, *, is_active: bool | None = None, skip: int = 0, limit: int = 100
    ) -> list[Warehouse]:
        query = db.query(Warehouse)
        if is_active is not None:
            query = query.filter(Warehouse.is_active == is_active)
        return (
            query.order_by(Warehouse.is_default.desc(), Warehouse.code.asc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count(self, db: Session, *, is_active: bool | None = None) -> int:
        query = db.query(Warehouse)
        if is_active is not None:
            query = query.filter(Warehouse.is_active == is_active)
        return query.count()

    def set_default(self, db: Session, *, warehouse: Warehouse) -> Warehouse:
        """Make ``warehouse`` the sole default, demoting any current default.

        The demotion runs under ``no_autoflush`` so ``warehouse``'s pending
        ``is_default=True`` is not flushed mid-operation (which would briefly put
        two ``True`` rows in the DB and trip the partial unique index). At the
        caller's commit only ``warehouse`` is ``True``.
        """
        with db.no_autoflush:
            db.query(Warehouse).filter(
                Warehouse.is_default.is_(True), Warehouse.id != warehouse.id
            ).update({Warehouse.is_default: False}, synchronize_session="fetch")
        warehouse.is_default = True
        db.add(warehouse)
        return warehouse

    def create(self, db: Session, *, obj_in: WarehouseCreate) -> Warehouse:
        """Create a warehouse. If ``is_default`` is requested, reassign the default
        after flushing so the new row has an id for the demotion's ``id !=`` guard."""
        warehouse = Warehouse(
            name=obj_in.name,
            code=obj_in.code,
            is_active=obj_in.is_active,
            is_default=False,
        )
        db.add(warehouse)
        db.flush()  # assign id before set_default's "id != warehouse.id" demotion
        if obj_in.is_default:
            self.set_default(db, warehouse=warehouse)
        return warehouse

    def update(self, db: Session, *, warehouse: Warehouse, obj_in: WarehouseUpdate) -> Warehouse:
        """Apply an update. ``is_default=True`` triggers ``set_default`` (kept in
        this method, not the route, so the route's ``audit.record_update`` still
        sees the change in ``get_history`` — ``set_default``'s demotion is
        ``no_autoflush``, so it doesn't clear the pending diff)."""
        data = obj_in.model_dump(exclude_unset=True)
        make_default = data.pop("is_default", None)
        for field, value in data.items():
            setattr(warehouse, field, value)
        if make_default is True:
            self.set_default(db, warehouse=warehouse)
        elif make_default is False:
            warehouse.is_default = False
        db.add(warehouse)
        return warehouse

    def has_stock(self, db: Session, *, warehouse_id: int) -> bool:
        """True if any stock_level row references this warehouse (delete guard)."""
        return (
            db.query(StockLevel.id).filter(StockLevel.warehouse_id == warehouse_id).first()
            is not None
        )

    def delete(self, db: Session, *, warehouse: Warehouse) -> None:
        db.delete(warehouse)


warehouse = CRUDWarehouse()
