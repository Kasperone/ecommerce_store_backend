"""Customer-facing company **team management** API (/api/v1/companies/me/users).

Wave D / B2B-2 slice 5. A company's ``COMPANY_ADMIN`` manages their company's
team: list members, invite by email + role, change a member's role/status
(promote/demote, suspend/reactivate), and remove a member. Everything is
owner-scoped to the *caller's own* company — there is no company id in the path,
the company is resolved from the authenticated caller's membership.

Authorisation (reusing the CU-be foundation):

- **GET** (list) — any **active** member may read the roster
  (``get_current_company_member``: a non-member 404s; a suspended/invited member
  doesn't resolve as active, so they 404 too).
- **POST / PATCH / DELETE** (mutations) — **COMPANY_ADMIN only**
  (``require_company_admin``: a BUYER/APPROVER/VIEWER member gets 403; a
  non-member 404s).

Two integrity guards on mutations:

- **Cross-company id → 404:** a ``{member_id}`` that isn't in the caller's
  company is reported as not-found (we never confirm another company's row
  exists).
- **Last COMPANY_ADMIN protected → 409:** a change that would drop the company's
  final active COMPANY_ADMIN out of the admin pool (demote, suspend, or remove)
  is refused — mirrors the admin Staff last-active-admin guard so a company can't
  be orphaned with no one able to manage it.

Invites work whether or not the address already has an account. An **existing
account** is linked directly; an address with **no account yet** gets a *pending
email-invite* (``company_users.user_id`` NULL + ``invited_email`` set) plus a
best-effort invite email, and is bound to the account when that address registers
(``link_pending_invites``, from the registration flow). A duplicate invite
(already a member, or an already-pending email) → 409.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core import email as email_service
from app.core.dependencies import (
    get_current_company_member,
    get_db,
    require_company_admin,
)
from app.crud import company_user as crud_company_user
from app.crud import user as crud_user
from app.crud.company_user import CompanyMembership
from app.models.company_user import CompanyMemberStatus, CompanyRole, CompanyUser
from app.schemas.company import (
    CompanyMemberInvite,
    CompanyMemberResponse,
    CompanyMemberUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _serialize(member: CompanyUser) -> CompanyMemberResponse:
    """Build the response for a membership row, denormalising the linked user.

    ``user_email`` / ``user_name`` come from the eager-loaded ``member.user`` so
    the FE roster renders without a second fetch. ``member.user`` is always set
    (the column is NOT NULL) but is guarded defensively.
    """
    linked = member.user
    return CompanyMemberResponse.model_validate(
        {
            "id": member.id,
            "created_at": member.created_at,
            "updated_at": member.updated_at,
            "company_id": member.company_id,
            "user_id": member.user_id,
            "role": member.role,
            "status": member.status,
            "invited_email": member.invited_email,
            "invited_at": member.invited_at,
            "user_email": linked.email if linked is not None else None,
            "user_name": linked.full_name if linked is not None else None,
        }
    )


def _get_member_in_company(db: Session, *, company_id: int, member_id: int) -> CompanyUser:
    """Fetch a membership row by id, scoped to ``company_id``, else 404.

    A row that exists but belongs to a *different* company is reported as
    not-found — the caller may only act on their own company's members and we
    don't leak that another company's membership id exists.
    """
    member = db.query(CompanyUser).filter(CompanyUser.id == member_id).first()
    if member is None or member.company_id != company_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    return member


def _is_active_admin(member: CompanyUser) -> bool:
    return (
        member.role == CompanyRole.COMPANY_ADMIN.value
        and member.status == CompanyMemberStatus.ACTIVE.value
    )


def _guard_last_admin(db: Session, *, member: CompanyUser, leaves_admin_pool: bool) -> None:
    """Refuse an op that would remove the company's last active COMPANY_ADMIN.

    Only relevant when ``member`` is *currently* an active COMPANY_ADMIN and the
    operation (demote / suspend / remove) would drop them out of the pool. If no
    *other* active admin remains → 409. Mirrors ``admin_staff`` (which uses 400);
    the team API uses **409 Conflict** per the TM-be contract.
    """
    if not (_is_active_admin(member) and leaves_admin_pool):
        return
    active_admins = crud_company_user.count_active_admins(db, company_id=member.company_id)
    # The target itself is counted; "no OTHER admin remains" means the count is 1.
    if active_admins <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot demote, suspend, or remove the last company admin",
        )


@router.get("", response_model=list[CompanyMemberResponse])
async def list_members(
    db: Session = Depends(get_db),
    membership: CompanyMembership = Depends(get_current_company_member),
):
    """List the caller's company's team (newest first). Any active member may read."""
    members = crud_company_user.list_for_company(db, company_id=membership.company.id)
    return [_serialize(m) for m in members]


@router.post("", response_model=CompanyMemberResponse, status_code=status.HTTP_201_CREATED)
async def invite_member(
    payload: CompanyMemberInvite,
    db: Session = Depends(get_db),
    membership: CompanyMembership = Depends(require_company_admin),
):
    """Invite someone (by email) to the caller's company with a B2B role (COMPANY_ADMIN only).

    Two cases, both creating an ``invited``-status membership (no access until
    activated) and returning it (201):

    - **Existing account** → the membership links to it. Already a member of this
      company → 409.
    - **No account yet** → a *pending email-invite* is staged (``user_id`` NULL,
      ``invited_email`` set) and an invite email is sent best-effort; the row is
      bound to the account when that address registers. A pending invite for the
      same email already exists → 409.
    """
    company_id = membership.company.id

    user = crud_user.get_by_email(db, email=payload.email)
    if user is not None:
        existing = crud_company_user.get_membership(db, company_id=company_id, user_id=user.id)
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User is already a member of this company",
            )
        member = crud_company_user.invite(
            db, company_id=company_id, user_id=user.id, email=payload.email, role=payload.role
        )
        db.commit()
        db.refresh(member)
        return _serialize(member)

    # No account yet → stage a pending email-invite, bound to the account on signup.
    if crud_company_user.get_email_invite(db, company_id=company_id, email=payload.email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An invite for that email is already pending",
        )
    member = crud_company_user.invite(
        db, company_id=company_id, user_id=None, email=payload.email, role=payload.role
    )
    db.commit()
    db.refresh(member)
    try:
        email_service.send_company_invite_email(
            email_to=payload.email,
            company_name=membership.company.name,
            role=payload.role,
        )
    except Exception:
        logger.warning(
            "Email send failed: kind=%s company_id=%s",
            "company_invite",
            company_id,
            exc_info=True,
        )
    return _serialize(member)


@router.patch("/{member_id}", response_model=CompanyMemberResponse)
async def update_member(
    member_id: int,
    payload: CompanyMemberUpdate,
    db: Session = Depends(get_db),
    membership: CompanyMembership = Depends(require_company_admin),
):
    """Change a member's role and/or status (COMPANY_ADMIN only).

    Promote/demote (``role``) and suspend/reactivate (``status``). The
    last-active-admin guard refuses a demote or suspend that would orphan the
    company (→ 409). A cross-company ``member_id`` → 404.
    """
    company_id = membership.company.id
    member = _get_member_in_company(db, company_id=company_id, member_id=member_id)

    data = payload.model_dump(exclude_unset=True)
    target_role = data.get("role", member.role)
    target_status = data.get("status", member.status)
    target_is_active_admin = (
        target_role == CompanyRole.COMPANY_ADMIN.value
        and target_status == CompanyMemberStatus.ACTIVE.value
    )
    # The op drops the member out of the active-admin pool if they're currently in
    # it and the merged target state is not an active COMPANY_ADMIN.
    _guard_last_admin(db, member=member, leaves_admin_pool=not target_is_active_admin)

    updated = crud_company_user.update_member(
        db,
        membership=member,
        role=data.get("role"),
        status=data.get("status"),
    )
    db.commit()
    db.refresh(updated)
    return _serialize(updated)


@router.delete("/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    member_id: int,
    db: Session = Depends(get_db),
    membership: CompanyMembership = Depends(require_company_admin),
):
    """Remove a member from the caller's company (COMPANY_ADMIN only).

    Refuses removing the company's last active COMPANY_ADMIN (→ 409). A
    cross-company ``member_id`` → 404. Returns 204 on success.
    """
    company_id = membership.company.id
    member = _get_member_in_company(db, company_id=company_id, member_id=member_id)

    # Removing a member always takes them out of the admin pool.
    _guard_last_admin(db, member=member, leaves_admin_pool=True)

    crud_company_user.remove(db, membership=member)
    db.commit()
    return None
