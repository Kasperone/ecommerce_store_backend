"""Admin B2B price-lists API — CRUD price lists + their negotiated entries.

All routes are admin-gated behind ``CUSTOMERS_WRITE`` (price lists are a B2B /
company construct, assigned to companies) and audited (resource_type
``price_list``). A price list is a header (name / active / notes) plus entries,
each pinning a per-currency price for a product (or a specific variant).

Assignment of a list to a company happens via the **company** admin update
(``PATCH /admin/companies/{id}`` with ``price_list_id``) — there is no assign
endpoint here.

Entry validation: the product must exist (404); a supplied ``variant_id`` must
belong to that product (422); the ``(list, product, variant)`` grain is unique —
a duplicate is a 409 (the caller should PATCH the existing entry).
"""

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.capabilities import CUSTOMERS_WRITE
from app.core.dependencies import get_current_admin, get_db, require_capability
from app.crud import price_list as crud_price_list
from app.crud import product as crud_product
from app.models.product_variant import ProductVariant
from app.models.user import User
from app.schemas.price_list import (
    PriceListCreate,
    PriceListEntryCreate,
    PriceListEntryResponse,
    PriceListEntryUpdate,
    PriceListListResponse,
    PriceListResponse,
    PriceListUpdate,
)
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

_require = Depends(require_capability(CUSTOMERS_WRITE))


# --- price-list header CRUD ------------------------------------------------


@router.get("/", response_model=PriceListListResponse)
async def list_price_lists(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, description="Case-insensitive substring on name"),
):
    """List price lists (newest first), searchable by name. Rows carry the entry
    count (no entries — fetch a single list for those)."""
    skip = (page - 1) * page_size
    rows = crud_price_list.get_multi(db, search=search, skip=skip, limit=page_size)
    total = crud_price_list.count(db, search=search)
    pages = ceil(total / page_size) if total > 0 else 1
    return {"items": rows, "total": total, "page": page, "page_size": page_size, "pages": pages}


@router.post(
    "/",
    dependencies=[_require],
    response_model=PriceListResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_price_list(
    payload: PriceListCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create an empty price list. Add entries via the entry endpoints."""
    row = crud_price_list.create(db, obj_in=payload)
    audit.record_create(resource_type="price_list", obj=row)
    db.commit()
    return crud_price_list.get_with_entries(db, row.id)


@router.get("/{price_list_id}", response_model=PriceListResponse)
async def get_price_list(
    price_list_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Get one price list with its nested entries."""
    row = crud_price_list.get_with_entries(db, price_list_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Price list not found")
    return row


@router.patch("/{price_list_id}", dependencies=[_require], response_model=PriceListResponse)
async def update_price_list(
    price_list_id: int,
    payload: PriceListUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update a price list header (partial). Deactivating a list (``is_active``
    false) makes it inert at checkout — assigned companies fall back to catalog."""
    row = crud_price_list.get(db, price_list_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Price list not found")
    crud_price_list.update(db, db_obj=row, obj_in=payload)
    audit.record_update(resource_type="price_list", obj=row)
    db.commit()
    return crud_price_list.get_with_entries(db, price_list_id)


@router.delete("/{price_list_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT)
async def delete_price_list(
    price_list_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a price list (its entries cascade). Any company assigned this list
    is unassigned (``companies.price_list_id`` → NULL via ON DELETE SET NULL)."""
    row = crud_price_list.get(db, price_list_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Price list not found")
    audit.record_delete(resource_type="price_list", obj=row)
    crud_price_list.delete(db, db_obj=row)
    db.commit()


# --- entry CRUD ------------------------------------------------------------


@router.post(
    "/{price_list_id}/entries",
    dependencies=[_require],
    response_model=PriceListEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_entry(
    price_list_id: int,
    payload: PriceListEntryCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Add a negotiated-price entry to a price list.

    404 if the list or product does not exist; 422 if ``variant_id`` is set but
    does not belong to the product; 409 if an entry already exists at this exact
    ``(product, variant)`` grain (PATCH it instead)."""
    price_list = crud_price_list.get(db, price_list_id)
    if price_list is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Price list not found")
    product = crud_product.get(db, payload.product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    if payload.variant_id is not None:
        variant = db.get(ProductVariant, payload.variant_id)
        if variant is None or variant.product_id != payload.product_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Variant does not belong to this product",
            )
    if (
        crud_price_list.find_entry(
            db,
            price_list_id=price_list_id,
            product_id=payload.product_id,
            variant_id=payload.variant_id,
        )
        is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An entry for this product/variant already exists in this list",
        )
    entry = crud_price_list.add_entry(db, price_list_id=price_list_id, obj_in=payload)
    audit.record_update(resource_type="price_list", obj=price_list)
    db.commit()
    return crud_price_list.get_entry(db, entry.id)


@router.patch(
    "/{price_list_id}/entries/{entry_id}",
    dependencies=[_require],
    response_model=PriceListEntryResponse,
)
async def update_entry(
    price_list_id: int,
    entry_id: int,
    payload: PriceListEntryUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update an entry's prices (partial). 404 if the entry is not in this list;
    422 if the change would clear all three currency prices (an entry must keep
    at least one)."""
    entry = crud_price_list.get_entry(db, entry_id)
    if entry is None or entry.price_list_id != price_list_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")
    data = payload.model_dump(exclude_unset=True)
    merged = {
        "price_usd": data.get("price_usd", entry.price_usd),
        "price_pln": data.get("price_pln", entry.price_pln),
        "price_eur": data.get("price_eur", entry.price_eur),
    }
    if all(v is None for v in merged.values()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one of price_usd / price_pln / price_eur must remain set",
        )
    crud_price_list.update_entry(db, db_obj=entry, obj_in=payload)
    audit.record_update(resource_type="price_list", obj=entry.price_list)
    db.commit()
    return crud_price_list.get_entry(db, entry_id)


@router.delete(
    "/{price_list_id}/entries/{entry_id}",
    dependencies=[_require],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_entry(
    price_list_id: int,
    entry_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Remove an entry (the product falls back to its catalog price for buyers on
    this list). 404 if the entry is not in this list."""
    entry = crud_price_list.get_entry(db, entry_id)
    if entry is None or entry.price_list_id != price_list_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")
    audit.record_update(resource_type="price_list", obj=entry.price_list)
    crud_price_list.delete_entry(db, db_obj=entry)
    db.commit()
