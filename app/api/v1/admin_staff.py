"""Admin staff API — manage staff (ADMIN-role) accounts (management only).

A thin view over the existing ``users`` table: list the admin roster and PATCH a
user's ``role`` (promote/demote between ``CUSTOMER`` / ``ADMIN``) and/or
``is_active``. All routes are admin-gated and audited (``resource_type="user"``).

Two safety guards protect against locking everyone out (both → 400):
- **Self-lockout:** the acting admin may not demote (role→CUSTOMER) or
  deactivate (is_active→False) their *own* account.
- **Last active admin:** an operation that would drop the target out of the
  active-admin pool is refused when no *other* active admin remains.

Inviting a brand-new admin by email is deferred — promotion of an existing user
(via the PATCH below) covers the MVP need, and there is no create endpoint here.
"""

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_admin, get_db
from app.crud import user as crud_user
from app.models.user import User, UserRole
from app.schemas.staff import StaffListResponse, StaffResponse, StaffUpdate
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

# Audit resource type for staff role/activation changes. The rows live in the
# same users table as customers, so ``"user"`` keeps the audit log coherent.
AUDIT_RESOURCE_TYPE = "user"


@router.get("/", response_model=StaffListResponse)
async def list_staff(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(
        None, max_length=100, description="Search email / first / last name"
    ),
):
    """List ADMIN-role users (the staff roster), newest first, paginated.

    ``search`` matches email / first name / last name (case-insensitive). The
    role filter is fixed to ADMIN — this endpoint is the admin roster, not a
    general user browser (use ``/admin/customers`` for that).
    """
    skip = (page - 1) * page_size
    admins = crud_user.get_multi_admin(
        db, role=UserRole.ADMIN, search=search, skip=skip, limit=page_size
    )
    total = crud_user.count_admin(db, role=UserRole.ADMIN, search=search)
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": admins,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.patch("/{user_id}", response_model=StaffResponse)
async def update_staff(
    user_id: int,
    payload: StaffUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Set a user's ``role`` and/or ``is_active`` (admin).

    Both guards run against the **merged** target state (a PATCH may send only
    one field): the acting admin can't demote/deactivate themselves, and the
    last active admin can't be demoted or deactivated.
    """
    user = crud_user.get(db, id=user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    data = payload.model_dump(exclude_unset=True)

    # Merged target state — only overwrite a field the caller actually sent.
    target_role = data.get("role", user.role)
    target_active = data.get("is_active", user.is_active)
    would_leave_admin_pool = target_role != UserRole.ADMIN or target_active is False

    # Self-lockout: an admin may not demote or deactivate their own account.
    if user.id == current_admin.id and would_leave_admin_pool:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot demote or deactivate your own account",
        )

    # Last-active-admin: refuse to drop the only remaining active admin out of
    # the pool. Only relevant when the target is *currently* an active admin and
    # the change would remove them from it.
    user_is_active_admin = user.role == UserRole.ADMIN and user.is_active
    if (
        user_is_active_admin
        and would_leave_admin_pool
        and crud_user.count_active_admins(db, exclude_id=user.id) == 0
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot demote or deactivate the last active admin",
        )

    if "role" in data:
        user.role = data["role"]
    if "is_active" in data:
        user.is_active = data["is_active"]
    db.add(user)
    audit.record_update(resource_type=AUDIT_RESOURCE_TYPE, obj=user)
    db.commit()
    db.refresh(user)
    return user
