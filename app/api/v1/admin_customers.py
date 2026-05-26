"""Admin customers API — a manage view over the users table.

All routes are admin-gated. Mutations are recorded to the audit log
(resource_type ``customer``). Lifetime order metrics are aggregated from the
orders table. Impersonation, tags, and admin notes are deliberately out of
scope (own future plans).
"""

import logging
from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core import email as email_service
from app.core.dependencies import get_current_admin, get_db
from app.crud import user as crud_user
from app.models.user import User, UserRole
from app.schemas.auth import GenericMessage
from app.schemas.customer import (
    CustomerAdminUpdate,
    CustomerDetail,
    CustomerListItem,
    CustomerListResponse,
    CustomerOrderMetrics,
)
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
):
    """List users with filters (admin). Newest first."""
    skip = (page - 1) * page_size
    users = crud_user.get_multi_admin(
        db,
        role=role,
        is_active=is_active,
        is_verified=is_verified,
        search=search,
        skip=skip,
        limit=page_size,
    )
    total = crud_user.count_admin(
        db, role=role, is_active=is_active, is_verified=is_verified, search=search
    )
    counts = crud_user.order_counts_for(db, user_ids=[u.id for u in users])
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
            shipping_country=u.shipping_country,
            order_count=counts.get(u.id, 0),
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
