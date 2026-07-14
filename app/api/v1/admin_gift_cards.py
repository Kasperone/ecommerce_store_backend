"""Admin gift-cards API — issue/list/inspect/manage stored-value cards.

All routes are admin-gated and audited (resource_type ``gift_card``), mirroring
``admin_coupons.py``. The plaintext code exists ONLY in the 201 issue response
(+ the issue email when ``recipient_email`` is given) — there is no resend
endpoint by design (D4: re-issue instead); every other shape carries ``last4``.

Balance is never PATCHable: the adjust endpoint is the only manual mutation and
every movement lands in the ``gift_card_entries`` ledger under the card-row
lock (see ``crud/gift_card.py``). A signed adjust that would take the balance
below zero is refused with 400 ``gift_card_balance_negative`` (machine code in
``detail`` — clients match on it, never on prose).
"""

import logging
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_admin, get_db
from app.core.money import to_money
from app.crud import gift_card as crud_gift_card
from app.crud import user as crud_user
from app.crud.gift_card import GiftCardError
from app.models.user import User
from app.schemas.gift_card import (
    GiftCardAdjustRequest,
    GiftCardDetailResponse,
    GiftCardEntryResponse,
    GiftCardIssueRequest,
    GiftCardIssueResponse,
    GiftCardListResponse,
    GiftCardResponse,
    GiftCardStatusLiteral,
    GiftCardUpdate,
)
from app.services.audit import AuditWriter, get_audit_writer
from app.services.gift_card_email import send_gift_card_email

logger = logging.getLogger(__name__)

router = APIRouter()


def _detail(card) -> GiftCardDetailResponse:
    """Build the detail shape (card + ledger newest-first)."""
    entries = sorted(card.entries, key=lambda e: (e.created_at, e.id), reverse=True)
    return GiftCardDetailResponse(
        **GiftCardResponse.model_validate(card).model_dump(),
        entries=[GiftCardEntryResponse.model_validate(e) for e in entries],
    )


@router.get("/", response_model=GiftCardListResponse)
async def list_gift_cards(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: GiftCardStatusLiteral | None = Query(
        None, alias="status", description="Filter by status"
    ),
    currency: str | None = Query(None, min_length=3, max_length=3),
    q: str | None = Query(
        None, max_length=100, description="Match last4 (exact) or recipient email (contains)"
    ),
):
    """List gift cards (newest first), filterable by status/currency/q."""
    skip = (page - 1) * page_size
    cards = crud_gift_card.get_multi(
        db, status=status_filter, currency=currency, q=q, skip=skip, limit=page_size
    )
    total = crud_gift_card.count(db, status=status_filter, currency=currency, q=q)
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": cards,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.post("/", response_model=GiftCardIssueResponse, status_code=status.HTTP_201_CREATED)
async def issue_gift_card(
    payload: GiftCardIssueRequest,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Issue a gift card. The 201 body is the ONLY response with the plaintext code.

    When ``recipient_email`` is set, the issue email (localized via ``locale``)
    goes out best-effort — a transport failure never fails the issue, because
    the admin already sees the code in the response and can pass it on.
    """
    if payload.user_id is not None and crud_user.get(db, id=payload.user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    card, code = crud_gift_card.issue(
        db,
        initial_value=to_money(payload.initial_value),  # type: ignore[arg-type] -- gt=0 in schema, never None
        currency=payload.currency,
        expires_at=payload.expires_at,
        recipient_email=payload.recipient_email,
        user_id=payload.user_id,
        note=payload.note,
        issued_by_user_id=current_admin.id,
    )
    # The snapshot carries code_hash (sha256) — never the plaintext.
    audit.record_create(resource_type="gift_card", obj=card)
    db.commit()
    db.refresh(card)

    if payload.recipient_email:
        try:
            send_gift_card_email(
                email_to=payload.recipient_email,
                code=code,
                amount=card.initial_value,
                currency=card.currency,
                expires_at=card.expires_at,
                lang=payload.locale,
            )
        except Exception:
            logger.warning(
                "Email send failed: kind=gift_card_issue gift_card_id=%s", card.id, exc_info=True
            )

    return GiftCardIssueResponse(
        **GiftCardResponse.model_validate(card).model_dump(),
        code=code,
    )


@router.get("/{gift_card_id}", response_model=GiftCardDetailResponse)
async def get_gift_card(
    gift_card_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Get one gift card with its ledger (newest first)."""
    card = crud_gift_card.get_with_entries(db, gift_card_id)
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gift card not found")
    return _detail(card)


@router.patch("/{gift_card_id}", response_model=GiftCardResponse)
async def update_gift_card(
    gift_card_id: int,
    payload: GiftCardUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update management fields (status / expires_at / note). Balance is not
    patchable — use the adjust endpoint so the ledger stays complete."""
    card = crud_gift_card.get(db, gift_card_id)
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gift card not found")
    crud_gift_card.update(
        db,
        card=card,
        status=payload.status,
        expires_at=payload.expires_at,
        note=payload.note,
        fields_set=payload.model_fields_set,
    )
    audit.record_update(resource_type="gift_card", obj=card)
    db.commit()
    db.refresh(card)
    return card


@router.post("/{gift_card_id}/adjust", response_model=GiftCardDetailResponse)
async def adjust_gift_card(
    gift_card_id: int,
    payload: GiftCardAdjustRequest,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Apply a signed manual balance correction (ledger kind ``adjust``).

    Resulting balance must stay >= 0 — else 400 ``gift_card_balance_negative``.
    """
    card = crud_gift_card.get(db, gift_card_id)
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gift card not found")
    try:
        card, _ = crud_gift_card.adjust(
            db,
            gift_card_id=gift_card_id,
            amount=to_money(payload.amount),  # type: ignore[arg-type] -- non-zero float, never None
            actor_user_id=current_admin.id,
            note=payload.note,
        )
    except GiftCardError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.code) from exc
    # The CRUD layer flushes (ledger row needs the lock window), so get_history
    # is already empty — record the movement explicitly instead of a diff.
    audit.record_event(
        action="update",
        resource_type="gift_card",
        resource_id=card.id,
        payload={
            "after": {
                "adjust_amount": payload.amount,
                "balance": float(card.balance),
                "note": payload.note,
            }
        },
    )
    db.commit()
    db.refresh(card)
    detail_card = crud_gift_card.get_with_entries(db, gift_card_id)
    if detail_card is None:  # pragma: no cover -- just committed
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gift card not found")
    return _detail(detail_card)
