"""Admin companies API — define/list/edit/delete B2B directory entries.

All routes are admin-gated and audited (resource_type ``company``). This is
**management metadata** — an approval workflow beyond the status field, and
linking customer users to companies, are separate, deferred concerns.

Validation: ``status`` is constrained to its allowed set by the schema literal
(``pending`` | ``approved`` | ``rejected``); ``name`` is required. There is no
cross-field rule, so PATCH does not need merged-state re-validation.

``companies.id`` (the integer PK) + ``CompanyResponse`` ``{ id, name, status }``
are the stable contract the quotes vertical FKs against.
"""

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_admin, get_db
from app.crud import company as crud_company
from app.models.user import User
from app.schemas.company import (
    CompanyCreate,
    CompanyListResponse,
    CompanyResponse,
    CompanyStatusLiteral,
    CompanyUpdate,
)
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()


@router.get("/", response_model=CompanyListResponse)
async def list_companies(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: CompanyStatusLiteral | None = Query(
        None, alias="status", description="Filter by directory status"
    ),
    search: str | None = Query(
        None, description="Filter by name or tax id (contains, case-insensitive)"
    ),
):
    """List companies (newest first), filterable by status and a name/tax-id substring."""
    skip = (page - 1) * page_size
    companies = crud_company.get_multi(
        db, status=status_filter, search=search, skip=skip, limit=page_size
    )
    total = crud_company.count(db, status=status_filter, search=search)
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": companies,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.post("/", response_model=CompanyResponse, status_code=status.HTTP_201_CREATED)
async def create_company(
    payload: CompanyCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a company. ``status`` defaults to ``pending`` (an unknown status 422s,
    enforced by the schema). A supplied ``primary_user_id`` must reference an existing
    user (422 otherwise)."""
    if payload.primary_user_id is not None and not crud_company.user_exists(
        db, payload.primary_user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Primary contact user not found",
        )
    company = crud_company.create(db, obj_in=payload)
    audit.record_create(resource_type="company", obj=company)
    db.commit()
    db.refresh(company)
    return company


@router.get("/{company_id}", response_model=CompanyResponse)
async def get_company(
    company_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Get one company by id."""
    company = crud_company.get(db, company_id)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return company


@router.patch("/{company_id}", response_model=CompanyResponse)
async def update_company(
    company_id: int,
    payload: CompanyUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update a company (partial). Only fields sent are applied; ``status`` stays
    constrained to the allowed set (422 on an unknown value, enforced by the schema)."""
    company = crud_company.get(db, company_id)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    # Validate a newly-set primary contact (a non-null value that was explicitly
    # sent). An explicit ``null`` clears the link; an omitted field leaves it.
    if (
        "primary_user_id" in payload.model_fields_set
        and payload.primary_user_id is not None
        and not crud_company.user_exists(db, payload.primary_user_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Primary contact user not found",
        )

    crud_company.update(db, company=company, obj_in=payload)
    audit.record_update(resource_type="company", obj=company)
    db.commit()
    db.refresh(company)
    return company


@router.delete("/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_company(
    company_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a company."""
    company = crud_company.get(db, company_id)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    audit.record_delete(resource_type="company", obj=company)
    crud_company.delete(db, company=company)
    db.commit()
