"""Admin coupons API — define/list/edit/delete discount codes (management only).

All routes are admin-gated and audited (resource_type ``coupon``). This is
**management metadata** — there is no redeem endpoint here and ``used_count`` is
read-only; applying a coupon to an order total is a separate, shared concern.

Validation: ``code`` is unique (stored UPPERCASE → 409 on duplicate); ``value``
is 1-100 for ``percent`` / ``> 0`` for ``fixed`` / exactly 0 for
``free_shipping``; ``currency`` is required for ``fixed`` and forbidden for
``percent`` and ``free_shipping``; ``ends_at`` must be after ``starts_at``.
The CREATE body validates itself (CouponCreate's ``model_validator``). PATCH
re-checks the per-type value/currency rule and the date window against the
**merged** state, because a partial update may flip one half of a pair (422 on a
conflict — mirrors the categories/inventory route convention). In particular,
flipping an existing fixed coupon to ``free_shipping`` must explicitly zero
``value`` and null ``currency`` in the same PATCH.
"""

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.capabilities import MARKETING_READ, MARKETING_WRITE
from app.core.dependencies import get_current_admin, get_db, require_capability, require_read
from app.crud import coupon as crud_coupon
from app.models.user import User
from app.schemas.coupon import (
    CouponCreate,
    CouponListResponse,
    CouponResponse,
    CouponUpdate,
    validate_value_and_currency,
    validate_window,
)
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

# Capability gate for this router's write/DELETE routes.
_require = Depends(require_capability(MARKETING_WRITE))
_read = Depends(require_read(MARKETING_READ))


@router.get("/", response_model=CouponListResponse)
async def list_coupons(
    db: Session = Depends(get_db),
    current_admin: User = _read,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    is_active: bool | None = Query(None, description="Filter by active status"),
    search: str | None = Query(None, description="Filter by code (contains, case-insensitive)"),
):
    """List coupons (newest first), filterable by active status and code substring."""
    skip = (page - 1) * page_size
    coupons = crud_coupon.get_multi(
        db, is_active=is_active, search=search, skip=skip, limit=page_size
    )
    total = crud_coupon.count(db, is_active=is_active, search=search)
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": coupons,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.post(
    "/", dependencies=[_require], response_model=CouponResponse, status_code=status.HTTP_201_CREATED
)
async def create_coupon(
    payload: CouponCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a coupon. ``code`` (stored UPPERCASE) must be unique → 409 on dup."""
    if crud_coupon.get_by_code(db, code=payload.code) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A coupon with code '{payload.code}' already exists",
        )
    coupon = crud_coupon.create(db, obj_in=payload)
    audit.record_create(resource_type="coupon", obj=coupon)
    db.commit()
    db.refresh(coupon)
    return coupon


@router.get("/{coupon_id}", response_model=CouponResponse)
async def get_coupon(
    coupon_id: int,
    db: Session = Depends(get_db),
    current_admin: User = _read,
):
    """Get one coupon by id."""
    coupon = crud_coupon.get(db, coupon_id)
    if coupon is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Coupon not found")
    return coupon


@router.patch("/{coupon_id}", dependencies=[_require], response_model=CouponResponse)
async def update_coupon(
    coupon_id: int,
    payload: CouponUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update a coupon. ``code`` stays unique (409 on collision). The per-type
    value/currency rule and the date window are re-validated against the merged
    state (422 on conflict)."""
    coupon = crud_coupon.get(db, coupon_id)
    if coupon is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Coupon not found")

    data = payload.model_dump(exclude_unset=True)

    if "code" in data and data["code"] != coupon.code:
        existing = crud_coupon.get_by_code(db, code=data["code"])
        if existing is not None and existing.id != coupon.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A coupon with code '{data['code']}' already exists",
            )

    # Re-validate against the merged state — a PATCH may change one half of a
    # value/currency or starts/ends pair without resending the other.
    merged_type = data.get("discount_type", coupon.discount_type)
    merged_value = data.get("value", coupon.value)
    merged_currency = data.get("currency", coupon.currency)
    merged_starts = data.get("starts_at", coupon.starts_at)
    merged_ends = data.get("ends_at", coupon.ends_at)
    try:
        validate_value_and_currency(merged_type, merged_value, merged_currency)
        validate_window(merged_starts, merged_ends)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    crud_coupon.update(db, coupon=coupon, obj_in=payload)
    audit.record_update(resource_type="coupon", obj=coupon)
    db.commit()
    db.refresh(coupon)
    return coupon


@router.delete("/{coupon_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT)
async def delete_coupon(
    coupon_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a coupon."""
    coupon = crud_coupon.get(db, coupon_id)
    if coupon is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Coupon not found")
    audit.record_delete(resource_type="coupon", obj=coupon)
    crud_coupon.delete(db, coupon=coupon)
    db.commit()
