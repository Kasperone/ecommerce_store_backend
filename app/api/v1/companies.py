"""Customer-facing companies API (/api/v1/companies).

Wave C / B2B-1. The only route is ``GET /companies/me`` — the caller's company,
resolved via ``companies.primary_user_id == caller.id`` (D-C-1/D-C-2). The
frontend uses it to show/hide the B2B surfaces (the "Request a quote" cart CTA +
the quotes nav). A caller who is no company's primary contact gets a 404 — they
are not a B2B account. Wave D generalizes the SAME route to ``company_users``
membership (forward-compatible — the contract stays "the caller's company or 404").
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_active_user, get_db
from app.crud import company as crud_company
from app.models.user import User
from app.schemas.company import CompanyResponse

router = APIRouter()


@router.get("/me", response_model=CompanyResponse)
async def get_my_company(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """The caller's company (resolved via primary contact), else 404.

    404 means "you are not a B2B account" — the FE uses it to gate the quote-request
    surfaces.
    """
    company = crud_company.get_for_primary_user(db, current_user.id)
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No company for this account"
        )
    return company
