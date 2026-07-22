"""Admin tax API — manage the tax vocabulary (categories + per-country rates).

Gated behind ``SETTINGS_WRITE``: tax categories and rates are store *reference*
data, sibling to shipping zones/rates, not product data. Assigning a product to
a category happens on the product form (``CATALOG_WRITE``), not here.

A **category** is a kind of goods; a **rate** is what that category attracts in
one country over one half-open validity window (``valid_from`` inclusive,
``valid_to`` exclusive, NULL = in force). Rate resolution lives in
``app/services/tax.py`` and is never on this write path.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.capabilities import SETTINGS_WRITE
from app.core.dependencies import get_current_admin, get_db, require_capability
from app.crud import tax_category as crud_tax_category
from app.crud import tax_rate as crud_tax_rate
from app.models.user import User
from app.schemas.tax import (
    TaxCategoryCreate,
    TaxCategoryResponse,
    TaxCategoryUpdate,
    TaxRateCreate,
    TaxRateResponse,
    TaxRateUpdate,
)
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

_require = Depends(require_capability(SETTINGS_WRITE))


# --- categories ------------------------------------------------------------


@router.get("/categories", response_model=list[TaxCategoryResponse])
async def list_tax_categories(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """All tax categories (default first), each with its rates nested — enough
    for the admin picker to label a category with its home-country rate."""
    return crud_tax_category.get_multi(db)


@router.post(
    "/categories",
    dependencies=[_require],
    response_model=TaxCategoryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_tax_category(
    payload: TaxCategoryCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a tax category. A duplicate ``code`` is rejected. Setting
    ``is_default`` demotes the previous default in the same transaction."""
    if crud_tax_category.get_by_code(db, code=payload.code):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A tax category with code '{payload.code}' already exists",
        )
    row = crud_tax_category.create(db, obj_in=payload)
    audit.record_create(resource_type="tax_category", obj=row)
    db.commit()
    return crud_tax_category.get(db, row.id)


@router.get("/categories/{category_id}", response_model=TaxCategoryResponse)
async def get_tax_category(
    category_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """One tax category with its rates."""
    row = crud_tax_category.get(db, category_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tax category not found")
    return row


@router.patch(
    "/categories/{category_id}", dependencies=[_require], response_model=TaxCategoryResponse
)
async def update_tax_category(
    category_id: int,
    payload: TaxCategoryUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update a tax category (partial). ``code`` is immutable. Promoting one to
    default demotes the incumbent. Demoting the only default (``is_default``
    false on the current default) is refused — a store must always have one."""
    row = crud_tax_category.get(db, category_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tax category not found")
    if payload.is_default is False and row.is_default:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot unset the default category; promote another category to default instead",
        )
    crud_tax_category.update(db, db_obj=row, obj_in=payload)
    audit.record_update(resource_type="tax_category", obj=row)
    db.commit()
    return crud_tax_category.get(db, category_id)


@router.delete(
    "/categories/{category_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_tax_category(
    category_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a tax category. Refused if any product is still classified under it
    (reclassify those products first) or if it is the store default — both would
    otherwise leave products un-taxable."""
    row = crud_tax_category.get(db, category_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tax category not found")
    if row.is_default:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete the default tax category; promote another category first",
        )
    in_use = crud_tax_category.products_using(db, category_id=category_id)
    if in_use:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete: {in_use} product(s) are classified under this category",
        )
    audit.record_delete(resource_type="tax_category", obj=row)
    crud_tax_category.delete(db, db_obj=row)
    db.commit()


# --- rates -----------------------------------------------------------------


@router.post(
    "/categories/{category_id}/rates",
    dependencies=[_require],
    response_model=TaxRateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_tax_rate(
    category_id: int,
    payload: TaxRateCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Add a rate to a category. A second rate for the same country and
    ``valid_from`` is a 409 (close the earlier window with a ``valid_to``
    instead of stacking duplicates)."""
    category = crud_tax_category.get(db, category_id)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tax category not found")
    if crud_tax_rate.find_conflict(
        db,
        category_id=category_id,
        country_code=payload.country_code,
        valid_from=payload.valid_from,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A rate for '{payload.country_code}' from {payload.valid_from.isoformat()} "
                "already exists in this category"
            ),
        )
    row = crud_tax_rate.create(db, category_id=category_id, obj_in=payload)
    audit.record_create(resource_type="tax_rate", obj=row)
    db.commit()
    return crud_tax_rate.get(db, row.id)


@router.patch("/rates/{rate_id}", dependencies=[_require], response_model=TaxRateResponse)
async def update_tax_rate(
    rate_id: int,
    payload: TaxRateUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update a rate (partial). The merged validity window and the
    ``(country, valid_from)`` uniqueness are checked against the stored row —
    neither is expressible on a partial schema alone."""
    row = crud_tax_rate.get(db, rate_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tax rate not found")
    data = payload.model_dump(exclude_unset=True)
    merged_country: str = data.get("country_code", row.country_code)
    merged_from: date = data.get("valid_from", row.valid_from)
    merged_to: date | None = data.get("valid_to", row.valid_to)
    if merged_to is not None and merged_to <= merged_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="valid_to must be after valid_from",
        )
    if crud_tax_rate.find_conflict(
        db,
        category_id=row.tax_category_id,
        country_code=merged_country,
        valid_from=merged_from,
        exclude_id=row.id,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A rate for '{merged_country}' from {merged_from.isoformat()} "
                "already exists in this category"
            ),
        )
    crud_tax_rate.update(db, db_obj=row, obj_in=payload)
    audit.record_update(resource_type="tax_rate", obj=row)
    db.commit()
    return crud_tax_rate.get(db, rate_id)


@router.delete("/rates/{rate_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT)
async def delete_tax_rate(
    rate_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a rate. A category left with no rate for a destination country will
    make orders to that country fail loudly at checkout (``TaxRateNotFoundError``)
    rather than under-charge — that is the intended safety, not a regression."""
    row = crud_tax_rate.get(db, rate_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tax rate not found")
    audit.record_delete(resource_type="tax_rate", obj=row)
    crud_tax_rate.delete(db, db_obj=row)
    db.commit()
