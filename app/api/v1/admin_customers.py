"""Admin customers API — a manage view over the users table.

All routes are admin-gated. Mutations are recorded to the audit log
(resource_type ``customer``). Lifetime order metrics are aggregated from the
orders table. Impersonation, tags, and admin notes are deliberately out of
scope (own future plans).
"""

import logging
from datetime import timedelta
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core import email as email_service
from app.core.config import settings
from app.core.dependencies import get_current_admin, get_db
from app.core.money import to_money
from app.core.rate_limit import limiter
from app.core.security import create_access_token
from app.crud import customer_note as crud_customer_note
from app.crud import store_credit as crud_store_credit
from app.crud import tag as crud_tag
from app.crud import user as crud_user
from app.crud.store_credit import StoreCreditError
from app.models.customer_note import CustomerNote
from app.models.user import User, UserRole
from app.schemas.auth import GenericMessage
from app.schemas.customer import (
    CustomerAdminUpdate,
    CustomerDetail,
    CustomerListItem,
    CustomerListResponse,
    CustomerOrderMetrics,
    ImpersonatedCustomer,
    ImpersonationResponse,
)
from app.schemas.customer_note import (
    CustomerNoteCreate,
    CustomerNoteResponse,
    CustomerNoteUpdate,
)
from app.schemas.store_credit import (
    StoreCreditAccountResponse,
    StoreCreditAdjustRequest,
    StoreCreditEntryResponse,
    StoreCreditSummaryResponse,
)
from app.schemas.tag import CustomerTagAssign, TagChip
from app.schemas.user import UserResponse
from app.services.audit import AuditWriter, get_audit_writer

logger = logging.getLogger(__name__)

router = APIRouter()

# Returned by the password-reset action regardless of the account's state, so
# the response can never be used to probe accounts and never leaks the token.
PASSWORD_RESET_TRIGGERED_MESSAGE = "A password reset email has been sent if delivery is configured."  # noqa: S105 -- user-facing message, not a credential


def _detail(db: Session, user: User) -> CustomerDetail:
    """Build the detail response (UserResponse fields + lockout + metrics)."""
    metrics = crud_user.order_metrics(db, user_id=user.id)
    return CustomerDetail(
        **UserResponse.model_validate(user).model_dump(),
        is_locked=user.is_locked,
        locked_until=user.locked_until,
        metrics=CustomerOrderMetrics(**metrics),
        tags=[TagChip.model_validate(t) for t in user.tags],
    )


@router.get("/", response_model=CustomerListResponse)
async def list_customers(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    role: UserRole | None = Query(None, description="Filter by role"),
    is_active: bool | None = Query(None, description="Filter by active state"),
    is_verified: bool | None = Query(None, description="Filter by verification state"),
    search: str | None = Query(
        None, max_length=100, description="Search email / first / last name"
    ),
    tag_id: int | None = Query(None, description="Filter by an assigned tag"),
):
    """List users with filters (admin). Newest first."""
    skip = (page - 1) * page_size
    users = crud_user.get_multi_admin(
        db,
        role=role,
        is_active=is_active,
        is_verified=is_verified,
        search=search,
        tag_id=tag_id,
        skip=skip,
        limit=page_size,
    )
    total = crud_user.count_admin(
        db,
        role=role,
        is_active=is_active,
        is_verified=is_verified,
        search=search,
        tag_id=tag_id,
    )
    user_ids = [u.id for u in users]
    counts = crud_user.order_counts_for(db, user_ids=user_ids)
    note_counts = crud_customer_note.note_counts_for(db, user_ids=user_ids)
    items = [
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
            note_count=note_counts.get(u.id, 0),
            tags=[TagChip.model_validate(t) for t in u.tags],
        )
        for u in users
    ]

    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.get("/{customer_id}", response_model=CustomerDetail)
async def get_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Get one customer with lifetime order metrics (admin)."""
    user = crud_user.get(db, id=customer_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return _detail(db, user)


@router.patch("/{customer_id}", response_model=CustomerDetail)
async def update_customer(
    customer_id: int,
    payload: CustomerAdminUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Toggle a customer's verification / active state (admin)."""
    user = crud_user.get(db, id=customer_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    # Self-lockout guard: an admin cannot disable or unverify their own account.
    if user.id == current_admin.id and (payload.is_active is False or payload.is_verified is False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot disable or unverify your own account",
        )

    if payload.is_verified is not None:
        user.is_verified = payload.is_verified
    if payload.is_active is not None:
        user.is_active = payload.is_active
    db.add(user)
    audit.record_update(resource_type="customer", obj=user)
    db.commit()
    db.refresh(user)
    return _detail(db, user)


@router.post("/{customer_id}/unlock", response_model=CustomerDetail)
async def unlock_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Clear a brute-force lockout (admin)."""
    user = crud_user.get(db, id=customer_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    crud_user.unlock(db, user=user)
    audit.record_update(resource_type="customer", obj=user)
    db.commit()
    db.refresh(user)
    return _detail(db, user)


@router.post("/{customer_id}/password-reset", response_model=GenericMessage)
async def send_customer_password_reset(
    customer_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Trigger the standard password-reset email for a customer (admin).

    Never returns the token (and never writes it to the audit log) — the token
    only travels in the emailed link, exactly like the self-service flow.
    """
    user = crud_user.get(db, id=customer_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    token = crud_user.issue_password_reset_token(db, user=user)  # commits internally
    try:
        email_service.send_password_reset_email(
            email_to=user.email,
            username=user.first_name or user.email,
            token=token,
        )
    except Exception:
        logger.warning("Email send failed: kind=password_reset user_id=%s", user.id, exc_info=True)

    # Record the admin action (no token in the payload — get_history is empty
    # after the token commit above, which is exactly what we want here).
    audit.record_update(resource_type="customer", obj=user)
    db.commit()
    return {"message": PASSWORD_RESET_TRIGGERED_MESSAGE}


@router.post("/{customer_id}/impersonate", response_model=ImpersonationResponse)
@limiter.limit("20/hour")
async def impersonate_customer(
    request: Request,
    customer_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Start a **view-only** impersonation session for a customer (admin).

    Issues a short-lived, no-refresh access token whose ``sub`` is the customer
    and which carries an ``impersonator_id`` claim. The write-guard middleware
    rejects every non-safe HTTP method on that token, so the admin can only *see*
    what the customer sees — never act for them. Refused for another admin (no
    impersonating peers) or an inactive account. Audited as ``impersonate``.

    Rate-limited (20/hour) and gated by the ``IMPERSONATION_ENABLED`` kill-switch
    so the capability can be throttled / disabled per environment.
    """
    if not settings.IMPERSONATION_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Customer impersonation is disabled",
        )
    user = crud_user.get(db, id=customer_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    if user.role == UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot impersonate another admin",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot impersonate an inactive customer",
        )

    token, _, expires_at = create_access_token(
        subject=str(user.id),
        expires_delta=timedelta(minutes=settings.IMPERSONATION_TOKEN_EXPIRE_MINUTES),
        impersonator_id=current_admin.id,
    )
    audit.record_event(
        action="impersonate",
        resource_type="customer",
        resource_id=user.id,
        payload={
            "after": {
                "impersonator_id": current_admin.id,
                "impersonator_email": current_admin.email,
                "customer_email": user.email,
            }
        },
    )
    db.commit()
    return ImpersonationResponse(
        access_token=token,
        expires_at=expires_at,
        customer=ImpersonatedCustomer(
            id=user.id,
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
        ),
    )


# ---------------------------------------------------------------------------
# Customer notes (internal CRM) — nested under /{customer_id}/notes.
# Audited as resource_type="customer_note". Any admin may edit/delete any note
# (small trusted team, every mutation recorded); author_id stays for attribution.
# ---------------------------------------------------------------------------


def _get_customer_or_404(db: Session, customer_id: int) -> User:
    user = crud_user.get(db, id=customer_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return user


def _get_note_or_404(db: Session, customer_id: int, note_id: int) -> CustomerNote:
    note = crud_customer_note.get(db, id=note_id)
    # Scope the note to its customer so /customers/{other}/notes/{id} 404s.
    if note is None or note.user_id != customer_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    return note


@router.get("/{customer_id}/notes", response_model=list[CustomerNoteResponse])
async def list_customer_notes(
    customer_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """List a customer's internal notes (admin), newest first."""
    _get_customer_or_404(db, customer_id)
    return crud_customer_note.get_for_customer(db, user_id=customer_id)


@router.post(
    "/{customer_id}/notes",
    response_model=CustomerNoteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_customer_note(
    customer_id: int,
    payload: CustomerNoteCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Add an internal note to a customer (admin). Author = the acting admin."""
    _get_customer_or_404(db, customer_id)
    note = crud_customer_note.create_for_customer(
        db, user_id=customer_id, author_id=current_admin.id, body=payload.body
    )
    audit.record_create(resource_type="customer_note", obj=note)
    db.commit()
    db.refresh(note)
    return note


@router.patch("/{customer_id}/notes/{note_id}", response_model=CustomerNoteResponse)
async def update_customer_note(
    customer_id: int,
    note_id: int,
    payload: CustomerNoteUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Edit a note's text (admin)."""
    note = _get_note_or_404(db, customer_id, note_id)
    crud_customer_note.update(db, db_obj=note, obj_in=payload)
    audit.record_update(resource_type="customer_note", obj=note)
    db.commit()
    db.refresh(note)
    return note


@router.delete("/{customer_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_customer_note(
    customer_id: int,
    note_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a note (admin)."""
    note = _get_note_or_404(db, customer_id, note_id)
    audit.record_delete(resource_type="customer_note", obj=note)
    db.delete(note)
    db.commit()


# ---------------------------------------------------------------------------
# Customer tags — assignment under /{customer_id}/tags. The vocabulary itself
# (create/rename/recolor/delete) lives in admin_tags.py. Assign/detach are
# audited via record_event (a join row has no clean ORM-object diff) as
# resource_type="customer_tag", resource_id=the customer.
# ---------------------------------------------------------------------------


@router.post("/{customer_id}/tags", response_model=list[TagChip])
async def assign_customer_tag(
    customer_id: int,
    payload: CustomerTagAssign,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Attach a tag to a customer (admin) — by ``tag_id`` or by ``name`` (created
    if new). Idempotent; returns the customer's full tag list."""
    user = _get_customer_or_404(db, customer_id)

    if payload.tag_id is not None:
        tag = crud_tag.get(db, id=payload.tag_id)
        if tag is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
    else:
        # name is guaranteed present + normalized by CustomerTagAssign.
        tag = crud_tag.get_by_name_ci(db, name=payload.name)  # type: ignore[arg-type]
        if tag is None:
            tag = crud_tag.create_tag(db, name=payload.name)  # type: ignore[arg-type]
            audit.record_create(resource_type="tag", obj=tag)

    if crud_tag.attach(db, user=user, tag=tag):
        audit.record_event(
            action="create",
            resource_type="customer_tag",
            resource_id=user.id,
            payload={"after": {"tag_id": tag.id, "name": tag.name}},
        )
    db.commit()
    db.refresh(user)
    return [TagChip.model_validate(t) for t in user.tags]


@router.delete("/{customer_id}/tags/{tag_id}", response_model=list[TagChip])
async def remove_customer_tag(
    customer_id: int,
    tag_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Detach a tag from a customer (admin). Idempotent; returns the remaining tags."""
    user = _get_customer_or_404(db, customer_id)
    tag = crud_tag.get(db, id=tag_id)
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")

    if crud_tag.detach(db, user=user, tag=tag):
        audit.record_event(
            action="delete",
            resource_type="customer_tag",
            resource_id=user.id,
            payload={"before": {"tag_id": tag.id, "name": tag.name}},
        )
    db.commit()
    db.refresh(user)
    return [TagChip.model_validate(t) for t in user.tags]


# ---------------------------------------------------------------------------
# Store credit — per-customer per-currency stored-value accounts (gift-cards +
# store-credit program). Balances only ever move through the ledger-writing
# CRUD (``crud/store_credit.py``, FOR-UPDATE + balance_after in one unit of
# work). Audited via record_event (resource_type="store_credit",
# resource_id=the customer) — an adjust is a movement, not an ORM diff.
# ---------------------------------------------------------------------------


def _store_credit_summary(db: Session, customer_id: int) -> StoreCreditSummaryResponse:
    """Shared summary shape (also served to the owner via /account/store-credit)."""
    accounts = crud_store_credit.accounts_for_user(db, user_id=customer_id)
    entries = crud_store_credit.entries_for_user(db, user_id=customer_id)
    return StoreCreditSummaryResponse(
        accounts=[StoreCreditAccountResponse.model_validate(a) for a in accounts],
        entries=[
            StoreCreditEntryResponse(
                id=entry.id,
                currency=currency,
                amount=float(entry.amount),
                kind=entry.kind,
                balance_after=float(entry.balance_after),
                order_id=entry.order_id,
                refund_id=entry.refund_id,
                note=entry.note,
                created_at=entry.created_at,
            )
            for entry, currency in entries
        ],
    )


@router.get("/{customer_id}/store-credit", response_model=StoreCreditSummaryResponse)
async def get_customer_store_credit(
    customer_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """A customer's store credit: per-currency balances + ledger, newest first."""
    _get_customer_or_404(db, customer_id)
    return _store_credit_summary(db, customer_id)


@router.post("/{customer_id}/store-credit/adjust", response_model=StoreCreditSummaryResponse)
async def adjust_customer_store_credit(
    customer_id: int,
    payload: StoreCreditAdjustRequest,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Apply a signed store-credit grant/correction (ledger kind ``adjust``).

    Creates the (customer, currency) account lazily on the first grant. A
    result below zero is refused with 400 ``store_credit_balance_negative``
    (machine code in ``detail``). Returns the refreshed summary.
    """
    _get_customer_or_404(db, customer_id)
    try:
        account, _ = crud_store_credit.adjust(
            db,
            user_id=customer_id,
            currency=payload.currency,
            amount=to_money(payload.amount),  # type: ignore[arg-type] -- non-zero float, never None
            actor_user_id=current_admin.id,
            note=payload.note,
        )
    except StoreCreditError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.code) from exc
    audit.record_event(
        action="update",
        resource_type="store_credit",
        resource_id=customer_id,
        payload={
            "after": {
                "currency": payload.currency,
                "adjust_amount": payload.amount,
                "balance": float(account.balance),
                "note": payload.note,
            }
        },
    )
    db.commit()
    return _store_credit_summary(db, customer_id)
