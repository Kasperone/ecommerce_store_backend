"""Admin inventory API — stock list, manual adjust, and the movement ledger.

All routes are admin-gated. The manual adjust mutates stock through
``crud_inventory.apply_delta`` (one ledger row per change) and is recorded to the
audit log (resource_type ``inventory``). Every stock change in the app — manual
adjust here plus the retrofitted order place/cancel paths — lands on the same
``inventory_movements`` ledger surfaced by ``GET /movements``.
"""

from datetime import date
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_admin, get_db
from app.crud import inventory as crud_inventory
from app.crud import product as crud_product
from app.crud import warehouse as crud_warehouse
from app.models.inventory_movement import InventoryMovement, InventoryMovementReason
from app.models.user import User
from app.schemas.inventory import (
    InventoryListItem,
    InventoryListResponse,
    InventoryMovementItem,
    InventoryMovementListResponse,
    InventoryStatus,
    RestockRequest,
    StockAdjustRequest,
    StockLevelItem,
    TransferRequest,
    TransferResponse,
)
from app.services import inventory_export, search_index
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()


def _export_filename(inventory_status: str | None, search: str | None) -> str:
    """``inventory`` base, suffixed with the active status / a search marker; falls
    back to ``inventory-{today}.csv`` when nothing is filtered."""
    parts = ["inventory"]
    if inventory_status is not None:
        parts.append(inventory_status)
    if search:
        parts.append("search")
    if len(parts) == 1:
        parts.append(date.today().isoformat())
    return "-".join(parts) + ".csv"


def _movement_item(
    movement: InventoryMovement, product_name: str, warehouse_name: str | None = None
) -> InventoryMovementItem:
    """Build a movement response row, joining in product + warehouse names."""
    return InventoryMovementItem(
        id=movement.id,
        created_at=movement.created_at,
        updated_at=movement.updated_at,
        product_id=movement.product_id,
        product_name=product_name,
        warehouse_id=movement.warehouse_id,
        warehouse_name=warehouse_name,
        delta=movement.delta,
        resulting_stock=movement.resulting_stock,
        reason=movement.reason,
        note=movement.note,
        order_id=movement.order_id,
        actor_user_id=movement.actor_user_id,
    )


@router.get("/", response_model=InventoryListResponse)
async def list_inventory(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    inventory_status: InventoryStatus | None = Query(
        None, alias="status", description="Filter by computed stock status"
    ),
    search: str | None = Query(None, max_length=100, description="Search name / SKU"),
):
    """List products with stock, threshold, and computed status (admin)."""
    skip = (page - 1) * page_size
    products = crud_inventory.get_multi_admin(
        db, status=inventory_status, search=search, skip=skip, limit=page_size
    )
    total = crud_inventory.count_admin(db, status=inventory_status, search=search)
    items = [
        InventoryListItem(
            id=p.id,
            name=p.name,
            sku=p.sku,
            stock=p.stock,
            low_stock_threshold=p.low_stock_threshold,
            status=crud_inventory.status_for(p),
        )
        for p in products
    ]
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.get(
    "/export",
    responses={200: {"content": {"text/csv": {}}, "description": "Inventory CSV export"}},
)
async def export_inventory(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    inventory_status: InventoryStatus | None = Query(None, alias="status"),
    search: str | None = Query(None, max_length=100),
):
    """Download the stock list as CSV (admin), inheriting the status/search filters.

    One row per product; per-warehouse levels packed into the ``warehouses`` column
    (default first, synthesised from unallocated stock where a level isn't
    materialised, so the packed levels sum to the ``stock`` total). RFC 4180, UTF-8
    with BOM. Generated on demand — no persistence, no audit row (read-only GET).
    """
    products = crud_inventory.get_for_export(db, status=inventory_status, search=search)
    default = crud_warehouse.get_default(db)
    levels_map = crud_inventory.levels_by_product(db, product_ids=[p.id for p in products])

    rows: list[inventory_export.InventoryExportRow] = []
    for product in products:
        levels = list(levels_map.get(product.id, []))
        # Synthesise the default entry from unallocated stock when not materialised
        # (mirrors crud_inventory.levels_for_product, but batched — no extra query).
        if default is not None and not any(wh.id == default.id for wh, _ in levels):
            materialised = sum(qty for _, qty in levels)
            levels.append((default, product.stock - materialised))
        levels.sort(key=lambda pair: (not pair[0].is_default, pair[0].code))
        rows.append(
            inventory_export.InventoryExportRow(
                product_id=product.id,
                name=product.name,
                sku=product.sku,
                stock=product.stock,
                low_stock_threshold=product.low_stock_threshold,
                status=crud_inventory.status_for(product),
                warehouses=[(wh.code, qty) for wh, qty in levels],
            )
        )

    csv_bytes = inventory_export.build_inventory_csv(rows)
    filename = _export_filename(inventory_status, search)
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/products/{product_id}/levels", response_model=list[StockLevelItem])
async def list_product_levels(
    product_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Per-warehouse stock breakdown for one product (admin).

    Always includes the default warehouse (synthesised from unallocated stock if
    its level row hasn't been materialised yet), so the rows sum to the product's
    total stock.
    """
    product = crud_product.get(db, id=product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return [
        StockLevelItem(
            warehouse_id=wh.id,
            warehouse_code=wh.code,
            warehouse_name=wh.name,
            quantity=qty,
            is_default=wh.is_default,
        )
        for wh, qty in crud_inventory.levels_for_product(db, product=product)
    ]


@router.patch("/products/{product_id}/stock", response_model=InventoryMovementItem)
async def adjust_stock(
    product_id: int,
    payload: StockAdjustRequest,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Set a product's stock in one warehouse to an absolute value (admin).

    Computes ``delta = new_stock - <that warehouse's level>``; a no-op (delta 0)
    is rejected so the ledger never gets empty rows. Records a
    ``manual_adjustment`` movement + an audit row in one transaction.
    """
    product = crud_product.get(db, id=product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    warehouse = crud_warehouse.get(db, payload.warehouse_id)
    if warehouse is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Warehouse not found")

    current_level = crud_inventory.get_level_quantity(
        db, product=product, warehouse_id=warehouse.id
    )
    delta = payload.new_stock - current_level
    if delta == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New stock equals current stock; nothing to adjust",
        )

    movement = crud_inventory.apply_delta(
        db,
        product=product,
        delta=delta,
        reason=InventoryMovementReason.MANUAL_ADJUSTMENT,
        warehouse_id=warehouse.id,
        actor_user_id=current_admin.id,
        note=payload.note,
    )
    audit.record_update(resource_type="inventory", obj=product)
    db.commit()
    db.refresh(movement)
    # Stock changed → refresh the in_stock facet (best-effort, admin path).
    search_index.sync_product(db, product_id)
    return _movement_item(movement, product.name, warehouse.name)


@router.post("/products/{product_id}/restock", response_model=InventoryMovementItem)
async def restock_product(
    product_id: int,
    payload: RestockRequest,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Add incoming stock to a warehouse (admin) — a supplier/purchase-order receipt.

    Records a ``restock`` movement of ``+quantity`` and an audit row in one
    transaction. 404 on unknown product/warehouse.
    """
    product = crud_product.get(db, id=product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    warehouse = crud_warehouse.get(db, payload.warehouse_id)
    if warehouse is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Warehouse not found")

    movement = crud_inventory.apply_delta(
        db,
        product=product,
        delta=payload.quantity,
        reason=InventoryMovementReason.RESTOCK,
        warehouse_id=warehouse.id,
        actor_user_id=current_admin.id,
        note=payload.note,
    )
    audit.record_update(resource_type="inventory", obj=product)
    db.commit()
    db.refresh(movement)
    # Stock rose → refresh the in_stock facet (best-effort, admin path).
    search_index.sync_product(db, product.id)
    return _movement_item(movement, product.name, warehouse.name)


@router.post("/transfer", response_model=TransferResponse)
async def transfer_stock(
    payload: TransferRequest,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Move stock between two warehouses (admin).

    Writes a paired transfer_out (source) + transfer_in (destination) movement in
    one transaction; the product total is unchanged. 404 on unknown
    product/warehouse, 422 on same-warehouse or insufficient source stock.
    """
    if payload.from_warehouse_id == payload.to_warehouse_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Source and destination warehouses must differ",
        )
    product = crud_product.get(db, id=payload.product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    source = crud_warehouse.get(db, payload.from_warehouse_id)
    dest = crud_warehouse.get(db, payload.to_warehouse_id)
    if source is None or dest is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Warehouse not found")

    available = crud_inventory.get_level_quantity(db, product=product, warehouse_id=source.id)
    if available < payload.quantity:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Insufficient stock in {source.code}: "
                f"have {available}, need {payload.quantity}"
            ),
        )

    out_movement = crud_inventory.apply_delta(
        db,
        product=product,
        delta=-payload.quantity,
        reason=InventoryMovementReason.TRANSFER_OUT,
        warehouse_id=source.id,
        actor_user_id=current_admin.id,
        note=payload.note,
    )
    in_movement = crud_inventory.apply_delta(
        db,
        product=product,
        delta=payload.quantity,
        reason=InventoryMovementReason.TRANSFER_IN,
        warehouse_id=dest.id,
        actor_user_id=current_admin.id,
        note=payload.note,
    )
    audit.record_event(
        action="transfer",
        resource_type="inventory",
        resource_id=product.id,
        payload={
            "transfer": {
                "product_id": product.id,
                "from_warehouse_id": source.id,
                "to_warehouse_id": dest.id,
                "quantity": payload.quantity,
            }
        },
    )
    db.commit()
    db.refresh(out_movement)
    db.refresh(in_movement)
    # Total unchanged, but the in_stock facet is unaffected anyway; sync is cheap.
    search_index.sync_product(db, product.id)
    return TransferResponse(
        product_id=product.id,
        from_warehouse_id=source.id,
        to_warehouse_id=dest.id,
        quantity=payload.quantity,
        from_resulting_stock=out_movement.resulting_stock,
        to_resulting_stock=in_movement.resulting_stock,
    )


@router.get("/movements", response_model=InventoryMovementListResponse)
async def list_movements(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    product_id: int | None = Query(None, description="Filter to one product's movements"),
    warehouse_id: int | None = Query(None, description="Filter to one warehouse's movements"),
):
    """Global stock-movement ledger, newest first; optional product/warehouse filters (admin)."""
    skip = (page - 1) * page_size
    rows = crud_inventory.movements_for(
        db, product_id=product_id, warehouse_id=warehouse_id, skip=skip, limit=page_size
    )
    total = crud_inventory.count_movements(db, product_id=product_id, warehouse_id=warehouse_id)
    items = [
        _movement_item(movement, product_name, warehouse_name)
        for movement, product_name, warehouse_name in rows
    ]
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }
