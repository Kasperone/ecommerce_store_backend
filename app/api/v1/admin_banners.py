"""Admin banners API — create/list/edit/delete storefront banners (management only).

All routes are admin-gated and audited (resource_type ``banner``). This is
**management metadata** — rendering banners on the storefront is a separate,
deferred concern; there is no public banners endpoint here.

Validation: ``placement`` must be in the allowed set (enforced by the schema's
``BannerPlacementLiteral`` → 422 on an unknown value); ``ends_at`` must be after
``starts_at``. The CREATE body validates itself (BannerCreate's
``model_validator``). PATCH re-checks the date window against the **merged**
state, because a partial update may set one half of the pair (422 on a conflict —
mirrors the coupons/categories route convention).
"""

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.capabilities import MARKETING_WRITE
from app.core.dependencies import get_current_admin, get_db, require_capability
from app.crud import banner as crud_banner
from app.models.user import User
from app.schemas.banner import (
    BannerCreate,
    BannerListResponse,
    BannerPlacementLiteral,
    BannerResponse,
    BannerUpdate,
    validate_window,
)
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

# Capability gate for this router's write/DELETE routes.
_require = Depends(require_capability(MARKETING_WRITE))


@router.get("/", response_model=BannerListResponse)
async def list_banners(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    is_active: bool | None = Query(None, description="Filter by active status"),
    placement: BannerPlacementLiteral | None = Query(None, description="Filter by placement"),
):
    """List banners (by sort_order then newest), filterable by active status and placement."""
    skip = (page - 1) * page_size
    banners = crud_banner.get_multi(
        db, is_active=is_active, placement=placement, skip=skip, limit=page_size
    )
    total = crud_banner.count(db, is_active=is_active, placement=placement)
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": banners,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.post(
    "/", dependencies=[_require], response_model=BannerResponse, status_code=status.HTTP_201_CREATED
)
async def create_banner(
    payload: BannerCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a banner."""
    banner = crud_banner.create(db, obj_in=payload)
    audit.record_create(resource_type="banner", obj=banner)
    db.commit()
    db.refresh(banner)
    return banner


@router.get("/{banner_id}", response_model=BannerResponse)
async def get_banner(
    banner_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Get one banner by id."""
    banner = crud_banner.get(db, banner_id)
    if banner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Banner not found")
    return banner


@router.patch("/{banner_id}", dependencies=[_require], response_model=BannerResponse)
async def update_banner(
    banner_id: int,
    payload: BannerUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update a banner. The date window is re-validated against the merged state
    (422 on conflict) — a PATCH may set one half of the starts/ends pair."""
    banner = crud_banner.get(db, banner_id)
    if banner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Banner not found")

    data = payload.model_dump(exclude_unset=True)

    # Re-validate against the merged state — a PATCH may change one half of the
    # starts/ends pair without resending the other.
    merged_starts = data.get("starts_at", banner.starts_at)
    merged_ends = data.get("ends_at", banner.ends_at)
    try:
        validate_window(merged_starts, merged_ends)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    crud_banner.update(db, banner=banner, obj_in=payload)
    audit.record_update(resource_type="banner", obj=banner)
    db.commit()
    db.refresh(banner)
    return banner


@router.delete("/{banner_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT)
async def delete_banner(
    banner_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a banner."""
    banner = crud_banner.get(db, banner_id)
    if banner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Banner not found")
    audit.record_delete(resource_type="banner", obj=banner)
    crud_banner.delete(db, banner=banner)
    db.commit()
