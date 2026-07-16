"""Customer-facing companies API (/api/v1/companies).

``GET /companies/me`` — the caller's company **resolved via ``company_users``
membership** (Wave D / D-D-2), plus the caller's B2B ``role``. The frontend uses
it to show/hide and role-gate the B2B surfaces (the "Request a quote" cart CTA,
the quotes nav, the company-profile + team sections). A caller who is no
company's member gets **200 with a null body** (not 404): the FE probes this on
every authed page, and a 404 there spams the browser console on every cold load.
404-for-non-member is reserved for the *intentional* B2B routes (team CRUD,
profile PATCH) where it's not a per-page probe.

Backward-compatible with Wave C (D-C-1/D-C-2): a company's denormalized primary
contact (``companies.primary_user_id``) with no explicit membership row resolves
identically as a ``COMPANY_ADMIN`` (see ``crud_company_user.get_active_membership``
+ the migration backfill), so the resolved company is unchanged and the existing
quote-request flow is unaffected.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_active_user, get_db, require_company_admin
from app.crud import company as crud_company
from app.crud import company_user as crud_company_user
from app.crud.company_user import CompanyMembership
from app.models.user import User
from app.schemas.company import (
    CompanyCreditResponse,
    CompanyMeResponse,
    CompanyProfileUpdate,
    CompanyResponse,
)
from app.services.b2b_net_terms import build_credit_summary

router = APIRouter()


@router.get("/me", response_model=CompanyMeResponse | None)
async def get_my_company(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """The caller's company + B2B role, or a **null body** if they're not B2B.

    Returns 200 with the company for a B2B account and **200 with ``null``** for
    a non-member — deliberately NOT 404. This is the caller asking about their
    OWN membership (not resource enumeration), and the FE probes it on every
    authed page to gate the B2B surfaces, so a 404 would spam the browser
    console on every cold load. The FE treats a null body as "not a B2B
    account". (Intentional routes — team CRUD, profile PATCH — keep 404 for
    non-members via ``get_current_company_member``/``require_company_admin``.)

    The response carries the caller's ``role`` so the FE can role-gate (VIEWER
    read-only; BUYER request quotes + checkout; COMPANY_ADMIN profile + team).
    Resolved via the active ``company_users`` membership; a denormalized primary
    contact with no membership row still resolves as COMPANY_ADMIN (Wave-C
    backward-compat — see ``crud_company_user.get_active_membership``).
    """
    membership = crud_company_user.get_active_membership(db, user_id=current_user.id)
    if membership is None:
        return None
    # ``role`` is merged onto the company fields via ``model_validate`` on a merged
    # dict so Pydantic coerces/validates the role string against ``CompanyRoleLiteral``
    # at runtime (``membership.role`` is a plain ``str``).
    base = CompanyResponse.model_validate(membership.company)
    return CompanyMeResponse.model_validate({**base.model_dump(), "role": membership.role})


@router.get("/me/credit", response_model=CompanyCreditResponse)
async def get_my_company_credit(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """The caller's company net-terms credit summary (limit / outstanding / available).

    Any active member may read it (net terms is a company-level relationship). A
    non-B2B caller gets a ``net_terms_enabled: false`` body (200, not 404) so the
    checkout probe stays quiet on non-B2B accounts — same posture as
    ``GET /companies/me``. The buyer uses it to see the pay-on-account option, the
    due term, and remaining credit at checkout.
    """
    membership = crud_company_user.get_active_membership(db, user_id=current_user.id)
    if membership is None:
        return CompanyCreditResponse(net_terms_enabled=False)
    return CompanyCreditResponse.model_validate(
        build_credit_summary(db, company=membership.company)
    )


@router.patch("/me", response_model=CompanyMeResponse)
async def update_my_company(
    payload: CompanyProfileUpdate,
    membership: CompanyMembership = Depends(require_company_admin),
    db: Session = Depends(get_db),
):
    """Update the caller's company profile (Wave D / D-D-4) — **COMPANY_ADMIN only**.

    ``require_company_admin`` gates this: a non-member 404s and a member who is
    not a COMPANY_ADMIN (BUYER/APPROVER/VIEWER) gets a 403. The editable fields
    are the narrow profile allow-list (contact + billing + ``default_currency``);
    ``name`` / ``status`` / ``primary_user_id`` are admin-directory concerns and
    are not exposed here. Returns the updated company with the caller's ``role``
    (same shape as ``GET /companies/me``)."""
    company = crud_company.update_profile(db, company=membership.company, obj_in=payload)
    db.commit()
    db.refresh(company)
    base = CompanyResponse.model_validate(company)
    return CompanyMeResponse.model_validate({**base.model_dump(), "role": membership.role})
