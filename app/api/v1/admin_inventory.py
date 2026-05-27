"""Admin inventory API — stock list, manual adjust, and the movement ledger.

All routes are admin-gated. The manual adjust mutates stock through
``crud_inventory.apply_delta`` (one ledger row per change) and is recorded to the
audit log (resource_type ``inventory``). Every stock change in the app — manual
adjust here plus the retrofitted order place/cancel paths — lands on the same
``inventory_movements`` ledger surfaced by ``GET /movements``.
"""

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_admin, get_db
from app.crud import inventory as crud_inventory
from app.crud import product as crud_product
from app.models.inventory_movement import InventoryMovement, InventoryMovementReason
from app.models.user import User
from app.schemas.inventory import (
    InventoryListItem,
    InventoryListResponse,
    InventoryMovementItem,
    InventoryMovementListResponse,
    InventoryStatus,
    StockAdjustRequest,
)
from app.services import search_index
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()


def _movement_item(movement: InventoryMovement, product_name: str) -> InventoryMovementItem:
    """Build a movement response row, joining in the product name for display."""
    return InventoryMovementItem(
        id=movement.id,
        created_at=movement.created_at,
        updated_at=movement.updated_at,
        product_id=movement.product_id,
        product_name=product_name,
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


@router.patch("/products/{product_id}/stock", response_model=InventoryMovementItem)
async def adjust_stock(
    product_id: int,
    payload: StockAdjustRequest,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Set a product's stock to an absolute value, recording the delta (admin).

    Computes ``delta = new_stock - current``; a no-op (delta 0) is rejected so the
    ledger never gets empty rows. Records a ``manual_adjustment`` movement + an
    audit row in one transaction.
    """
    product = crud_product.get(db, id=product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    delta = payload.new_stock - product.stock
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
        actor_user_id=current_admin.id,
        note=payload.note,
    )
    audit.record_update(resource_type="inventory", obj=product)
    db.commit()
    db.refresh(movement)
    # Stock changed → refresh the in_stock facet (best-effort, admin path).
    search_index.sync_product(db, product_id)
    return _movement_item(movement, product.name)


@router.get("/movements", response_model=InventoryMovementListResponse)
async def list_movements(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    product_id: int | None = Query(None, description="Filter to one product's movements"),
):
    """Global stock-movement ledger, newest first; optional product filter (admin)."""
    skip = (page - 1) * page_size
    rows = crud_inventory.movements_for(db, product_id=product_id, skip=skip, limit=page_size)
    total = crud_inventory.count_movements(db, product_id=product_id)
    items = [_movement_item(movement, product_name) for movement, product_name in rows]
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }
