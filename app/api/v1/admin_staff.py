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

from app.core.capabilities import STAFF_READ, STAFF_WRITE
from app.core.dependencies import get_current_admin, get_db, require_capability, require_read
from app.crud import user as crud_user
from app.models.user import User, UserRole
from app.schemas.staff import StaffListResponse, StaffResponse, StaffUpdate
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

# Capability gate for this router's write/DELETE routes.
_require = Depends(require_capability(STAFF_WRITE))
_read = Depends(require_read(STAFF_READ))

# Audit resource type for staff role/activation changes. The rows live in the
# same users table as customers, so ``"user"`` keeps the audit log coherent.
AUDIT_RESOURCE_TYPE = "user"


@router.get("/", response_model=StaffListResponse)
async def list_staff(
    db: Session = Depends(get_db),
    current_admin: User = _read,
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


@router.patch("/{user_id}", dependencies=[_require], response_model=StaffResponse)
async def update_staff(
    user_id: int,
    payload: StaffUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Set a user's ``role`` / ``is_active`` / ``is_owner`` / ``capabilities`` (admin).

    All guards run against the **merged** target state (a PATCH may send only
    one field):

    - **Privilege escalation** — only an *owner* may set/clear ``is_owner``,
      change ``capabilities``, or edit an account that is *currently* an owner.
      A scoped ``staff:write`` holder can only flip role/activation on
      non-owner accounts. (This endpoint is already gated ``staff:write``.)
    - **Self-lockout** — the acting admin can't demote/deactivate themselves.
    - **Last active admin** — can't drop the only remaining active admin.
    - **Last active owner** — can't remove the only remaining active owner
      (owners are the only accounts that can manage staff/capabilities).

    A demotion to ``CUSTOMER`` also clears ``is_owner`` and ``capabilities`` —
    those are staff-only and meaningless off an admin account.
    """
    user = crud_user.get(db, id=user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    data = payload.model_dump(exclude_unset=True)

    # --- Privilege-escalation guard ---------------------------------------
    # Owner-only surface: touching the owner flag, any capability grant, or any
    # account that is already an owner. A non-owner staffer (even with
    # staff:write) may not cross this line.
    touches_owner_surface = "is_owner" in data or "capabilities" in data or user.is_owner
    if touches_owner_surface and not current_admin.is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an owner can manage owners and capabilities",
        )

    # Merged target state — only overwrite a field the caller actually sent.
    target_role = data.get("role", user.role)
    target_active = data.get("is_active", user.is_active)
    target_is_owner = data.get("is_owner", user.is_owner)
    target_caps = data.get("capabilities", list(user.capabilities or []))
    # A non-admin account can't be an owner or hold capabilities.
    if target_role != UserRole.ADMIN:
        target_is_owner = False
        target_caps = []

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

    # Last-active-owner: refuse to drop the only remaining active owner. Fires
    # when the target is currently an active owner and the merged state removes
    # them from the owner pool (owner flag cleared, deactivated, or demoted).
    user_is_active_owner = user.is_owner and user.is_active
    would_leave_owner_pool = not target_is_owner or target_active is False
    if (
        user_is_active_owner
        and would_leave_owner_pool
        and crud_user.count_active_owners(db, exclude_id=user.id) == 0
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove the last active owner",
        )

    if "role" in data:
        user.role = data["role"]
    if "is_active" in data:
        user.is_active = data["is_active"]
    # Apply the normalized owner/capability state (also clears them on demotion).
    # Only write on an actual change so no-op PATCHes don't pollute the audit diff.
    if user.is_owner != target_is_owner:
        user.is_owner = target_is_owner
    if list(user.capabilities or []) != target_caps:
        user.capabilities = target_caps
    db.add(user)
    audit.record_update(resource_type=AUDIT_RESOURCE_TYPE, obj=user)
    db.commit()
    db.refresh(user)
    return user
