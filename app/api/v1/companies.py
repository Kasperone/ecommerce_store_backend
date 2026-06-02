"""Customer-facing companies API (/api/v1/companies).

``GET /companies/me`` — the caller's company **resolved via ``company_users``
membership** (Wave D / D-D-2), plus the caller's B2B ``role``. The frontend uses
it to show/hide and role-gate the B2B surfaces (the "Request a quote" cart CTA,
the quotes nav, the company-profile + team sections). A caller who is no
company's member gets a **404** — they are not a B2B account.

Backward-compatible with Wave C (D-C-1/D-C-2): a company's denormalized primary
contact (``companies.primary_user_id``) with no explicit membership row resolves
identically as a ``COMPANY_ADMIN`` (see ``crud_company_user.get_active_membership``
+ the migration backfill), so the contract stays "the caller's company or 404"
and the existing quote-request flow is unaffected.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_company_member, get_db, require_company_admin
from app.crud import company as crud_company
from app.crud.company_user import CompanyMembership
from app.schemas.company import (
    CompanyMeResponse,
    CompanyProfileUpdate,
    CompanyResponse,
)

router = APIRouter()


@router.get("/me", response_model=CompanyMeResponse)
async def get_my_company(
    membership: CompanyMembership = Depends(get_current_company_member),
):
    """The caller's company + B2B role (resolved via membership), else 404.

    404 means "you are not a B2B account" — the FE uses it to gate the B2B
    surfaces. The response carries the caller's ``role`` so the FE can role-gate
    (VIEWER read-only; BUYER request quotes + checkout; COMPANY_ADMIN profile +
    team).
    """
    # The company fields come from the ORM row via ``from_attributes``; ``role`` is
    # the resolved membership role merged in. Going through ``model_validate`` on a
    # merged dict lets Pydantic coerce/validate the role string against the
    # ``CompanyRoleLiteral`` at runtime (it's DB-CHECK-guaranteed, but this keeps
    # the type honest — ``membership.role`` is a plain ``str``).
    base = CompanyResponse.model_validate(membership.company)
    return CompanyMeResponse.model_validate({**base.model_dump(), "role": membership.role})


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
