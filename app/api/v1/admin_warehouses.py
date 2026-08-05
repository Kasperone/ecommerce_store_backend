"""Admin warehouses API — manage stock locations + the default fulfilment warehouse.

All routes are admin-gated and audited (resource_type ``warehouse``). Stock
quantities are not set here — they live per-warehouse on ``stock_levels`` and
change only through ``crud_inventory.apply_delta`` (the inventory endpoints).
A warehouse holding any stock cannot be deleted (409); deactivate it instead.
"""

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.capabilities import INVENTORY_READ, INVENTORY_WRITE
from app.core.dependencies import get_current_admin, get_db, require_capability, require_read
from app.crud import warehouse as crud_warehouse
from app.models.user import User
from app.schemas.warehouse import (
    WarehouseCreate,
    WarehouseListResponse,
    WarehouseResponse,
    WarehouseUpdate,
)
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

# Capability gate for this router's write/DELETE routes.
_require = Depends(require_capability(INVENTORY_WRITE))
_read = Depends(require_read(INVENTORY_READ))


@router.get("/", response_model=WarehouseListResponse)
async def list_warehouses(
    db: Session = Depends(get_db),
    current_admin: User = _read,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    is_active: bool | None = Query(None, description="Filter by active status"),
):
    """List warehouses (default first, then by code)."""
    skip = (page - 1) * page_size
    warehouses = crud_warehouse.get_multi(db, is_active=is_active, skip=skip, limit=page_size)
    total = crud_warehouse.count(db, is_active=is_active)
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": warehouses,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.post(
    "/",
    dependencies=[_require],
    response_model=WarehouseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_warehouse(
    payload: WarehouseCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a warehouse. ``code`` must be unique; ``is_default=true`` reassigns
    the default."""
    if crud_warehouse.get_by_code(db, code=payload.code) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A warehouse with code '{payload.code}' already exists",
        )
    warehouse = crud_warehouse.create(db, obj_in=payload)
    audit.record_create(resource_type="warehouse", obj=warehouse)
    db.commit()
    db.refresh(warehouse)
    return warehouse


@router.get("/{warehouse_id}", response_model=WarehouseResponse)
async def get_warehouse(
    warehouse_id: int,
    db: Session = Depends(get_db),
    current_admin: User = _read,
):
    """Get one warehouse by id."""
    warehouse = crud_warehouse.get(db, warehouse_id)
    if warehouse is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Warehouse not found")
    return warehouse


@router.patch("/{warehouse_id}", dependencies=[_require], response_model=WarehouseResponse)
async def update_warehouse(
    warehouse_id: int,
    payload: WarehouseUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update a warehouse. ``code`` stays unique; ``is_default=true`` reassigns the
    default. The current default cannot be un-defaulted directly (set another as
    default instead) — that would leave no fulfilment warehouse."""
    warehouse = crud_warehouse.get(db, warehouse_id)
    if warehouse is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Warehouse not found")

    if payload.code is not None and payload.code != warehouse.code:
        existing = crud_warehouse.get_by_code(db, code=payload.code)
        if existing is not None and existing.id != warehouse.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A warehouse with code '{payload.code}' already exists",
            )
    if payload.is_default is False and warehouse.is_default:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set another warehouse as the default instead of un-setting this one",
        )

    crud_warehouse.update(db, warehouse=warehouse, obj_in=payload)
    audit.record_update(resource_type="warehouse", obj=warehouse)
    db.commit()
    db.refresh(warehouse)
    return warehouse


@router.delete("/{warehouse_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT)
async def delete_warehouse(
    warehouse_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a warehouse. Blocked (409) if it holds any stock or is the default."""
    warehouse = crud_warehouse.get(db, warehouse_id)
    if warehouse is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Warehouse not found")
    if warehouse.is_default:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete the default warehouse",
        )
    if crud_warehouse.has_stock(db, warehouse_id=warehouse.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete a warehouse that holds stock; deactivate it instead",
        )
    audit.record_delete(resource_type="warehouse", obj=warehouse)
    crud_warehouse.delete(db, warehouse=warehouse)
    db.commit()
