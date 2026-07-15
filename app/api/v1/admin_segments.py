"""Admin customer-segments API — define/list/edit/delete + preview membership.

All routes are admin-gated and audited (resource_type ``segment``). The CRUD
routes manage stored segment definitions; the two preview routes resolve a
filter to a live membership count + sample:

- ``GET /{id}/preview`` resolves a **saved** segment's stored filters.
- ``POST /preview`` resolves an **unsaved** ``SegmentFilters`` body (for the
  admin filter-builder's live "Matches N customers").

The resolver (``crud_segment.resolve_user_ids`` / ``preview``) is set-based SQL
— EXISTS + correlated scalar subqueries over ``customer_tags`` / grouped order
aggregates / the default-shipping address — never a per-customer Python loop.

Mirrors ``admin_campaigns.py`` (the CRUD shape, audit posture, and pagination).
"""

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.capabilities import CUSTOMERS_WRITE
from app.core.dependencies import get_current_admin, get_db, require_capability
from app.crud import segment as crud_segment
from app.crud import user as crud_user
from app.models.segment import Segment
from app.models.user import User
from app.schemas.customer import CustomerListItem
from app.schemas.segment import (
    SegmentCreate,
    SegmentFilters,
    SegmentListResponse,
    SegmentPreview,
    SegmentResponse,
    SegmentUpdate,
)
from app.schemas.tag import TagChip
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

# Capability gate for this router's write/DELETE routes.
_require = Depends(require_capability(CUSTOMERS_WRITE))


def _to_list_items(db: Session, users: list[User]) -> list[CustomerListItem]:
    """Build the ``CustomerListItem`` sample for a preview (mirrors the admin
    customer list — order counts in one grouped query, tags from the eager load)."""
    user_ids = [u.id for u in users]
    counts = crud_user.order_counts_for(db, user_ids=user_ids)
    return [
        CustomerListItem(
            id=u.id,
            created_at=u.created_at,
            updated_at=u.updated_at,
            email=u.email,
            first_name=u.first_name,
            last_name=u.last_name,
            role=u.role,
            is_active=u.is_active,
            is_verified=u.is_verified,
            is_locked=u.is_locked,
            company_name=u.company_name,
            order_count=counts.get(u.id, 0),
            tags=[TagChip.model_validate(t) for t in u.tags],
        )
        for u in users
    ]


def _preview(db: Session, filters: SegmentFilters, *, limit: int) -> SegmentPreview:
    count, sample = crud_segment.preview(db, filters, limit=limit)
    return SegmentPreview(count=count, sample=_to_list_items(db, sample))


@router.get("/", response_model=SegmentListResponse)
async def list_segments(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    is_active: bool | None = Query(None, description="Filter by active toggle"),
    search: str | None = Query(None, description="Filter by name (contains, case-insensitive)"),
):
    """List segments (newest first), filterable by active toggle and name substring."""
    skip = (page - 1) * page_size
    segments = crud_segment.get_multi(
        db, is_active=is_active, search=search, skip=skip, limit=page_size
    )
    total = crud_segment.count(db, is_active=is_active, search=search)
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": segments,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.post(
    "/",
    dependencies=[_require],
    response_model=SegmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_segment(
    payload: SegmentCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a segment. ``filters`` is validated against ``SegmentFilters``
    (``extra="forbid"``; ``min_total_spent`` requires a ``currency``)."""
    segment = crud_segment.create(db, obj_in=payload)
    audit.record_create(resource_type="segment", obj=segment)
    db.commit()
    db.refresh(segment)
    return segment


@router.post("/preview", dependencies=[_require], response_model=SegmentPreview)
async def preview_unsaved_segment(
    filters: SegmentFilters,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    limit: int = Query(20, ge=1, le=100),
):
    """Resolve an **unsaved** filter body to a live membership count + sample —
    powers the filter-builder's "Matches N customers" preview before saving."""
    return _preview(db, filters, limit=limit)


@router.get("/{segment_id}", response_model=SegmentResponse)
async def get_segment(
    segment_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Get one segment by id."""
    segment = crud_segment.get(db, segment_id)
    if segment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segment not found")
    return segment


@router.patch("/{segment_id}", dependencies=[_require], response_model=SegmentResponse)
async def update_segment(
    segment_id: int,
    payload: SegmentUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update a segment (partial). Only fields explicitly sent are touched."""
    segment = crud_segment.get(db, segment_id)
    if segment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segment not found")
    crud_segment.update(db, segment=segment, obj_in=payload)
    audit.record_update(resource_type="segment", obj=segment)
    db.commit()
    db.refresh(segment)
    return segment


@router.get("/{segment_id}/preview", response_model=SegmentPreview)
async def preview_saved_segment(
    segment_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    limit: int = Query(20, ge=1, le=100),
):
    """Resolve a **saved** segment's stored filters to a live count + sample."""
    segment = crud_segment.get(db, segment_id)
    if segment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segment not found")
    filters = SegmentFilters.model_validate(segment.filters or {})
    return _preview(db, filters, limit=limit)


@router.delete("/{segment_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT)
async def delete_segment(
    segment_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a segment."""
    segment: Segment | None = crud_segment.get(db, segment_id)
    if segment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segment not found")
    audit.record_delete(resource_type="segment", obj=segment)
    crud_segment.delete(db, segment=segment)
    db.commit()
