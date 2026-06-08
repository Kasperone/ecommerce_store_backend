"""Admin campaigns API — define/list/edit/delete + send marketing email campaigns.

All routes are admin-gated and audited (resource_type ``campaign``). The
CRUD routes manage **metadata**; the ``POST /{id}/send`` route activates a
campaign — it resolves the audience and dispatches the email best-effort via
Resend, then flips the campaign to ``sent`` and stamps ``sent_at`` (which is
read-only everywhere else — clients can't set it through create/update).

OUTWARD-FACING: ``send`` dispatches real email. Tests mock
``app.core.email.send_email`` — no live sends in pytest.

Validation: ``audience`` / ``status`` are constrained to their allowed sets by
the schema literals; ``scheduled_at`` is required when ``status`` is
``scheduled``. The CREATE body validates itself (CampaignCreate's
``model_validator``). PATCH re-checks the schedule rule against the **merged**
state, because a partial update may set ``status=scheduled`` without resending
``scheduled_at`` (422 on a conflict — mirrors the coupons/banners route
convention).
"""

import logging
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core import email as email_mod
from app.core.dependencies import get_current_admin, get_db
from app.crud import campaign as crud_campaign
from app.crud.segment import segment as crud_segment
from app.models.user import User
from app.schemas.campaign import (
    CampaignAudienceLiteral,
    CampaignCreate,
    CampaignListResponse,
    CampaignResponse,
    CampaignStatusLiteral,
    CampaignUpdate,
    validate_schedule,
    validate_segment_audience,
)
from app.services.audit import AuditWriter, get_audit_writer

logger = logging.getLogger(__name__)

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
    requires ``scheduled_at`` (422 otherwise, enforced by the schema). An
    ``audience='segment'`` campaign requires a ``segment_id`` that references an
    existing segment (422 / 404 otherwise)."""
    if payload.segment_id is not None and crud_segment.get(db, payload.segment_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segment not found")
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
    # Same merged-state re-check for the segment invariant: a PATCH may flip the
    # audience or the segment_id without resending the other (mirrors the DB CHECK).
    merged_audience = data.get("audience", campaign.audience)
    merged_segment_id = data.get("segment_id", campaign.segment_id)
    try:
        validate_schedule(merged_status, merged_scheduled_at)
        validate_segment_audience(merged_audience, merged_segment_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    if merged_segment_id is not None and crud_segment.get(db, merged_segment_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Segment not found")

    crud_campaign.update(db, campaign=campaign, obj_in=payload)
    audit.record_update(resource_type="campaign", obj=campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


@router.post("/{campaign_id}/send", response_model=CampaignResponse)
async def send_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Send a campaign to its resolved audience, then mark it ``sent``.

    Only ``draft`` / ``scheduled`` campaigns may send (409 if already ``sent``).
    The audience (``all`` / ``customers`` / ``b2b`` / ``segment``) is resolved to
    recipient emails, then each is mailed **best-effort** inside a per-recipient
    try/except — one bad address never aborts the run (mirrors the order-email
    best-effort posture); failures are logged. On completion the campaign flips
    to ``sent`` with ``sent_at`` stamped, alongside an audit row, in one
    transaction.

    OUTWARD-FACING: this dispatches real email via Resend. Tests mock
    ``app.core.email.send_email`` — no live sends in pytest.
    """
    campaign = crud_campaign.get(db, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
    if campaign.status == "sent":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="campaign already sent")

    recipients = crud_campaign.resolve_recipient_emails(
        db, audience=campaign.audience, segment_id=campaign.segment_id
    )
    sent_ok = 0
    failed = 0
    for recipient in recipients:
        try:
            email_mod.send_email(
                email_to=recipient,
                subject=campaign.subject,
                html_content=campaign.body or "",
            )
            sent_ok += 1
        except Exception:
            failed += 1
            logger.warning(
                "Campaign send failed: campaign_id=%s recipient=%s",
                campaign.id,
                recipient,
                exc_info=True,
            )

    logger.info(
        "Campaign %s sent: audience=%s recipients=%s ok=%s failed=%s",
        campaign.id,
        campaign.audience,
        len(recipients),
        sent_ok,
        failed,
    )

    crud_campaign.mark_sent(db, campaign=campaign)
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
