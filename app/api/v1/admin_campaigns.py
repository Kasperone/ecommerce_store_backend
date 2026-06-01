"""Admin campaigns API — define/list/edit/delete marketing email campaigns.

All routes are admin-gated and audited (resource_type ``campaign``). This is
**management metadata** — there is no send endpoint here and ``sent_at`` is
read-only; actually delivering the email / executing audience segmentation is a
separate, deferred concern.

Validation: ``audience`` / ``status`` are constrained to their allowed sets by
the schema literals; ``scheduled_at`` is required when ``status`` is
``scheduled``. The CREATE body validates itself (CampaignCreate's
``model_validator``). PATCH re-checks the schedule rule against the **merged**
state, because a partial update may set ``status=scheduled`` without resending
``scheduled_at`` (422 on a conflict — mirrors the coupons/banners route
convention).
"""

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_admin, get_db
from app.crud import campaign as crud_campaign
from app.models.user import User
from app.schemas.campaign import (
    CampaignAudienceLiteral,
    CampaignCreate,
    CampaignListResponse,
    CampaignResponse,
    CampaignStatusLiteral,
    CampaignUpdate,
    validate_schedule,
)
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()


@router.get("/", response_model=CampaignListResponse)
async def list_campaigns(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: CampaignStatusLiteral | None = Query(
        None, alias="status", description="Filter by lifecycle status"
    ),
    audience: CampaignAudienceLiteral | None = Query(None, description="Filter by audience"),
    search: str | None = Query(None, description="Filter by name (contains, case-insensitive)"),
):
    """List campaigns (newest first), filterable by status, audience, and name substring."""
    skip = (page - 1) * page_size
    campaigns = crud_campaign.get_multi(
        db, status=status_filter, audience=audience, search=search, skip=skip, limit=page_size
    )
    total = crud_campaign.count(db, status=status_filter, audience=audience, search=search)
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": campaigns,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.post("/", response_model=CampaignResponse, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    payload: CampaignCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a campaign. ``status`` defaults to ``draft``; a ``scheduled`` status
    requires ``scheduled_at`` (422 otherwise, enforced by the schema)."""
    campaign = crud_campaign.create(db, obj_in=payload)
    audit.record_create(resource_type="campaign", obj=campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


@router.get("/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Get one campaign by id."""
    campaign = crud_campaign.get(db, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
    return campaign


@router.patch("/{campaign_id}", response_model=CampaignResponse)
async def update_campaign(
    campaign_id: int,
    payload: CampaignUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update a campaign. The ``scheduled``-requires-``scheduled_at`` rule is
    re-validated against the merged state (422 on conflict), because a PATCH may
    flip the status or clear the schedule without resending the other field."""
    campaign = crud_campaign.get(db, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")

    data = payload.model_dump(exclude_unset=True)

    # Re-validate against the merged state — a PATCH may set status=scheduled
    # without resending scheduled_at (or clear scheduled_at on a scheduled one).
    merged_status = data.get("status", campaign.status)
    merged_scheduled_at = data.get("scheduled_at", campaign.scheduled_at)
    try:
        validate_schedule(merged_status, merged_scheduled_at)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    crud_campaign.update(db, campaign=campaign, obj_in=payload)
    audit.record_update(resource_type="campaign", obj=campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


@router.delete("/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a campaign."""
    campaign = crud_campaign.get(db, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
    audit.record_delete(resource_type="campaign", obj=campaign)
    crud_campaign.delete(db, campaign=campaign)
    db.commit()
