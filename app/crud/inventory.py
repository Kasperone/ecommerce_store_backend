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

from sqlalchemy import and_, func, not_, or_
from sqlalchemy.orm import Session

from app.crud.warehouse import warehouse as crud_warehouse
from app.models.inventory_movement import InventoryMovement, InventoryMovementReason
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.warehouse import StockLevel, Warehouse
from app.schemas.inventory import InventoryStatus


class CRUDInventory:
    """Stock-mutation choke-point + admin read helpers (no commit; caller owns the txn)."""

    def _get_or_create_level(
        self,
        db: Session,
        *,
        product: Product,
        warehouse_id: int,
        is_default: bool,
        variant: ProductVariant | None = None,
    ) -> StockLevel:
        """Return the (product, warehouse, variant) stock level, creating it if absent.

        A new **default**-warehouse *product-grain* level is seeded from the
        product's *unallocated* stock — ``product.stock`` minus what already sits
        elsewhere — so a product that has stock but no materialised level (one
        created before this table existed, or via a test factory) reconciles to
        its total. Non-default levels seed at 0. **Variant-grain levels always
        seed at 0** — there is no unallocated synthesis at variant grain; variant
        levels are materialised explicitly at seeding/restock time. The new row
        is flushed so a later ``apply_delta`` in the same unit of work (e.g. a
        multi-line order on the same product) finds it instead of inserting a
        duplicate.

        Called **before** the caller mutates ``product.stock``, so this flush
        can't clear a pending ``product.stock`` change from ``get_history`` (the
        manual-adjust route's audit reads that diff after ``apply_delta`` returns).
        """
        variant_filter = (
            StockLevel.variant_id == variant.id
            if variant is not None
            else StockLevel.variant_id.is_(None)
        )
        level = (
            db.query(StockLevel)
            .filter(
                StockLevel.product_id == product.id,
                StockLevel.warehouse_id == warehouse_id,
                variant_filter,
            )
            .first()
        )
        if level is not None:
            return level
        seed = (
            self._default_unallocated(db, product=product, default_id=warehouse_id)
            if is_default and variant is None
            else 0
        )
        level = StockLevel(
            product_id=product.id,
            warehouse_id=warehouse_id,
            variant_id=variant.id if variant is not None else None,
            quantity=seed,
        )
        db.add(level)
        db.flush()
        return level

    def _default_unallocated(self, db: Session, *, product: Product, default_id: int) -> int:
        """The product's stock not accounted for by any other materialised level.

        ``product.stock`` (total) minus the sum of every level **except the
        default product-grain one** — i.e. non-default levels at any grain plus
        the default warehouse's variant-grain levels. This is what the default
        warehouse implicitly holds at product grain before its level is
        materialised — the seed for a new default level and the read for
        ``get_level_quantity``. (Subtracting variant rows keeps the synthesis at
        0 for a variant product whose product-grain default level was never
        materialised — variant stock is never "unallocated".)
        """
        other = (
            db.query(func.coalesce(func.sum(StockLevel.quantity), 0))
            .filter(
                StockLevel.product_id == product.id,
                not_(
                    and_(
                        StockLevel.warehouse_id == default_id,
                        StockLevel.variant_id.is_(None),
                    )
                ),
            )
            .scalar()
        )
        return product.stock - (other or 0)

    def get_level_quantity(
        self,
        db: Session,
        *,
        product: Product,
        warehouse_id: int,
        variant: ProductVariant | None = None,
    ) -> int:
        """Current quantity in a (product, warehouse[, variant]) level WITHOUT
        materialising it.

        Read-only twin of ``_get_or_create_level``'s seed: a not-yet-materialised
        product-grain default level reports the product's unallocated stock; a
        missing non-default level reports 0. **A missing variant-grain level
        always reports 0** — variant levels are materialised explicitly, never
        synthesised. Used by the order availability check (orders fulfil from the
        default warehouse, so the check is against that level, not the product
        total).
        """
        variant_filter = (
            StockLevel.variant_id == variant.id
            if variant is not None
            else StockLevel.variant_id.is_(None)
        )
        level = (
            db.query(StockLevel)
            .filter(
                StockLevel.product_id == product.id,
                StockLevel.warehouse_id == warehouse_id,
                variant_filter,
            )
            .first()
        )
        if level is not None:
            return level.quantity
        if variant is not None:
            return 0
        default = crud_warehouse.get_default(db)
        if default is not None and warehouse_id == default.id:
            return self._default_unallocated(db, product=product, default_id=warehouse_id)
        return 0

    def apply_delta(
        self,
        db: Session,
        *,
        product: Product,
        delta: int,
        reason: InventoryMovementReason,
        warehouse_id: int | None = None,
        variant: ProductVariant | None = None,
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
        warehouse's level after the delta). With ``variant`` set, the mutation
        happens at variant grain and ``variant.stock`` is maintained in the same
        lock-step (all three aggregates move together — no extra flushes in this
        branch; the audit writer reads ``get_history`` after this returns). Does
        **not** commit or flush past the get-or-create — the caller's transaction
        owns the boundary so the order/audit row and the stock change land or
        roll back together.

        Raises ``ValueError`` if the warehouse level would go negative, unless
        ``allow_negative`` (the backorder path). Real callers validate first and
        surface a proper 4xx; this is the defense-in-depth guard.
        """
        default = crud_warehouse.get_default(db)
        if default is None:
            raise RuntimeError("No default warehouse configured; run migrations")
        target_id = warehouse_id if warehouse_id is not None else default.id

        level = self._get_or_create_level(
            db,
            product=product,
            warehouse_id=target_id,
            is_default=(target_id == default.id),
            variant=variant,
        )
        new_level = level.quantity + delta
        if new_level < 0 and not allow_negative:
            grain = f" (variant {variant.id})" if variant is not None else ""
            raise ValueError(
                f"Stock for product {product.id}{grain} in warehouse {target_id} cannot go "
                f"negative (current {level.quantity}, delta {delta:+d})"
            )
        level.quantity = new_level
        if variant is not None:
            variant.stock = variant.stock + delta
            db.add(variant)
        product.stock = product.stock + delta
        movement = InventoryMovement(
            product_id=product.id,
            warehouse_id=target_id,
            variant_id=variant.id if variant is not None else None,
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
        """Stock status: ``backorder`` (<=0 and backorder allowed) / ``out_of_stock``
        (<=0, no backorder) / ``low_stock`` (at or below a set threshold, but >0) /
        ``in_stock``. A null threshold means never "low"."""
        if product.stock <= 0:
            return "backorder" if product.allow_backorder else "out_of_stock"
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
            query = query.filter(Product.stock <= 0, Product.allow_backorder.is_(False))
        elif status == "backorder":
            query = query.filter(Product.stock <= 0, Product.allow_backorder.is_(True))
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

    def get_for_export(
        self, db: Session, *, status: str | None = None, search: str | None = None
    ) -> list[Product]:
        """Every product matching the status/search filters (no pagination) for CSV
        export — so the export mirrors the on-screen stock list."""
        return (
            self._admin_filtered_query(db, status=status, search=search)
            .order_by(Product.name.asc(), Product.id.asc())
            .all()
        )

    def levels_by_product(
        self, db: Session, *, product_ids: list[int]
    ) -> dict[int, list[tuple[Warehouse, int]]]:
        """Materialised per-warehouse levels for many products in one query
        (``{product_id: [(warehouse, quantity), ...]}``). Quantities are summed
        across grains (product-grain NULL rows + any variant rows) so a variant
        product's warehouse total stays one entry. Avoids the per-product N+1 of
        ``levels_for_product`` for the CSV; the caller synthesises the default
        entry where it isn't materialised."""
        result: dict[int, list[tuple[Warehouse, int]]] = {pid: [] for pid in product_ids}
        if not product_ids:
            return result
        rows = (
            db.query(StockLevel.product_id, Warehouse, func.sum(StockLevel.quantity))
            .join(Warehouse, StockLevel.warehouse_id == Warehouse.id)
            .filter(StockLevel.product_id.in_(product_ids))
            .group_by(StockLevel.product_id, Warehouse.id)
            .all()
        )
        for product_id, wh, quantity in rows:
            result[product_id].append((wh, int(quantity)))
        return result

    # ------------------------------------------------------------------
    # Movement log
    # ------------------------------------------------------------------

    def levels_for_product(self, db: Session, *, product: Product) -> list[tuple[Warehouse, int]]:
        """Per-warehouse stock for a product as ``(warehouse, quantity)``.

        Every materialised level joined to its warehouse (summed across grains —
        product-grain NULL rows plus any variant rows), plus a synthesised
        default-warehouse entry (from unallocated stock) when no default level
        has been materialised yet — so the breakdown always accounts for the
        full ``product.stock``. Default first, then by code.
        """
        default = crud_warehouse.get_default(db)
        rows = (
            db.query(Warehouse, func.sum(StockLevel.quantity))
            .join(StockLevel, StockLevel.warehouse_id == Warehouse.id)
            .filter(StockLevel.product_id == product.id)
            .group_by(Warehouse.id)
            .all()
        )
        levels: list[tuple[Warehouse, int]] = [(wh, int(qty)) for wh, qty in rows]
        if default is not None and not any(wh.id == default.id for wh, _ in levels):
            levels.append(
                (default, self._default_unallocated(db, product=product, default_id=default.id))
            )
        levels.sort(key=lambda pair: (not pair[0].is_default, pair[0].code))
        return levels

    def levels_for_product_variants(
        self, db: Session, *, product: Product
    ) -> list[tuple[Warehouse, ProductVariant, int]]:
        """Materialised **variant-grain** levels for a product as
        ``(warehouse, variant, quantity)``.

        No synthesis at variant grain — variant levels are always materialised
        explicitly (seeded via ``apply_delta`` at variant-creation/restock
        time), so the rows are the complete picture. Default warehouse first,
        then by code, then variant position.
        """
        rows = (
            db.query(StockLevel, Warehouse, ProductVariant)
            .join(Warehouse, StockLevel.warehouse_id == Warehouse.id)
            .join(ProductVariant, StockLevel.variant_id == ProductVariant.id)
            .filter(StockLevel.product_id == product.id)
            .all()
        )
        levels = [(wh, variant, lvl.quantity) for lvl, wh, variant in rows]
        levels.sort(key=lambda row: (not row[0].is_default, row[0].code, row[1].position))
        return levels

    def movements_for(
        self,
        db: Session,
        *,
        product_id: int | None = None,
        warehouse_id: int | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> list[tuple[InventoryMovement, str, str | None]]:
        """Movement log rows as ``(movement, product_name, warehouse_name)``, newest first.

        Joins ``Product`` for the product name and left-joins ``Warehouse`` for the
        warehouse name (a movement's ``warehouse_id`` is SET NULL if the warehouse
        was later deleted, so the join must be outer).
        """
        query = (
            db.query(InventoryMovement, Product.name, Warehouse.name)
            .join(Product, InventoryMovement.product_id == Product.id)
            .outerjoin(Warehouse, InventoryMovement.warehouse_id == Warehouse.id)
        )
        if product_id is not None:
            query = query.filter(InventoryMovement.product_id == product_id)
        if warehouse_id is not None:
            query = query.filter(InventoryMovement.warehouse_id == warehouse_id)
        rows = (
            query.order_by(InventoryMovement.created_at.desc(), InventoryMovement.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )
        return [(movement, pname, wname) for movement, pname, wname in rows]

    def count_movements(
        self, db: Session, *, product_id: int | None = None, warehouse_id: int | None = None
    ) -> int:
        query = db.query(InventoryMovement)
        if product_id is not None:
            query = query.filter(InventoryMovement.product_id == product_id)
        if warehouse_id is not None:
            query = query.filter(InventoryMovement.warehouse_id == warehouse_id)
        return query.count()


inventory = CRUDInventory()
