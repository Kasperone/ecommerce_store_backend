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

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_active_user, get_db, require_company_admin
from app.crud import company as crud_company
from app.crud import company_user as crud_company_user
from app.crud.company_user import CompanyMembership
from app.models.user import User
from app.schemas.company import (
    AutoChargeConfirmRequest,
    AutoChargeSetupIntentResponse,
    AutoChargeStatusResponse,
    CompanyCreditResponse,
    CompanyMeResponse,
    CompanyProfileUpdate,
    CompanyResponse,
)
from app.services import vies
from app.services.b2b_net_terms import build_credit_summary
from app.services.stripe import get_stripe_service

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


@router.post("/me/auto-charge/setup-intent", response_model=AutoChargeSetupIntentResponse)
async def create_auto_charge_setup_intent(
    membership: CompanyMembership = Depends(require_company_admin),
    db: Session = Depends(get_db),
):
    """Start vaulting a card for net-terms auto-pay — **COMPANY_ADMIN only**.

    Ensures a Stripe Customer for the company, then returns a SetupIntent client
    secret the portal confirms with the Payment Element (setup mode). 503 when
    Stripe is unconfigured. The card is stored only after ``/confirm``.
    """
    service = get_stripe_service()
    company = membership.company
    if not company.stripe_customer_id:
        company.stripe_customer_id = service.create_customer(
            email=company.email, name=company.name, ext_ref=f"company:{company.id}"
        )
        db.add(company)
        db.commit()
    intent = service.create_setup_intent(customer_id=company.stripe_customer_id)
    return AutoChargeSetupIntentResponse(client_secret=intent["client_secret"])


@router.post("/me/auto-charge/confirm", response_model=AutoChargeStatusResponse)
async def confirm_auto_charge_card(
    payload: AutoChargeConfirmRequest,
    membership: CompanyMembership = Depends(require_company_admin),
    db: Session = Depends(get_db),
):
    """Store the vaulted PaymentMethod from a confirmed SetupIntent — COMPANY_ADMIN.

    Only a ``succeeded`` SetupIntent whose customer matches the company's is
    accepted (400 otherwise), so a client can't point us at an unrelated card.
    """
    service = get_stripe_service()
    company = membership.company
    info = service.retrieve_setup_intent(setup_intent_id=payload.setup_intent_id)
    if info["status"] != "succeeded" or not info["payment_method"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="SetupIntent not completed"
        )
    if (
        company.stripe_customer_id
        and info["customer"]
        and info["customer"] != company.stripe_customer_id
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="SetupIntent customer mismatch"
        )
    company.auto_charge_payment_method_id = info["payment_method"]
    if info["customer"]:
        company.stripe_customer_id = info["customer"]
    db.add(company)
    db.commit()
    return AutoChargeStatusResponse(auto_charge_card_set=True)


@router.delete("/me/auto-charge", response_model=AutoChargeStatusResponse)
async def remove_auto_charge_card(
    membership: CompanyMembership = Depends(require_company_admin),
    db: Session = Depends(get_db),
):
    """Remove the vaulted card — COMPANY_ADMIN. Auto-pay stops; net terms falls
    back to manual bank transfer. Idempotent (no card set → still ``false``)."""
    company = membership.company
    company.auto_charge_payment_method_id = None
    db.add(company)
    db.commit()
    return AutoChargeStatusResponse(auto_charge_card_set=False)


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


@router.post("/me/vies-check", response_model=CompanyMeResponse)
async def check_my_company_vies(
    membership: CompanyMembership = Depends(require_company_admin),
    db: Session = Depends(get_db),
):
    """Validate the caller's company VAT-ID against VIES — **COMPANY_ADMIN only**.

    The self-service counterpart to the admin ``/admin/companies/{id}/vies-check``
    (Phase-0 tax U3): a company validates its own EU VAT-ID during onboarding, off
    the checkout path. 503 when VIES is disabled; 422 when no VAT-ID is set; 502
    when VIES is temporarily unreachable (the attempt is still recorded)."""
    if not settings.VIES_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="VIES validation is disabled",
        )
    company = membership.company
    try:
        vies.validate_company_vat_id(db, company)
    except vies.VIESValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except vies.VIESUnavailableError as exc:
        db.commit()  # persist the recorded attempt (vies_last_checked_at)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="VIES is temporarily unavailable; please try again later",
        ) from exc
    db.commit()
    db.refresh(company)
    base = CompanyResponse.model_validate(company)
    return CompanyMeResponse.model_validate({**base.model_dump(), "role": membership.role})
