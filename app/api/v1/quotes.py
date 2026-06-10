"""Customer-facing quotes API (/api/v1/quotes).

Wave C / B2B-1, **membership-aware as of the F5 fix**. Any *active* member of a
company (``company_users`` row, or the denormalized ``primary_user_id`` contact
which resolves as ``COMPANY_ADMIN``) can:

- **request** a quote from their cart (``POST /quotes``) — the server prices each
  line from the CURRENT product price in the requested currency (D-C-4, mirrors the
  coupon re-pricing discipline; a client-sent price is never trusted);
- **list / view** their company's quotes (company-scoped — another company's quote
  is a 404, existence not leaked);
- **accept** a ``sent`` quote (``POST /quotes/{id}/accept`` → ``accepted``; any other
  source state → 409, D-C-5);
- **convert** an ``accepted`` quote into a pending order (``POST /quotes/{id}/convert``).
  The order is owned by the company's **primary contact** (``primary_user_id``) —
  independent of which member converts — so order history, emails and invoices
  attribute to a real customer; quoted prices honoured, stock decremented,
  ``shipping``/``discount``/``tax`` are 0. Wrong state → 422, already converted → 409;
- **download** a quote PDF (``GET /quotes/{id}/quote.pdf?lang=en|pl``).

The caller's company + B2B role is resolved via the same membership path as
``GET /companies/me`` (``get_active_membership``) — this closes finding F5, where the
quote routes used ``companies.primary_user_id`` only and locked non-primary members
(BUYER/APPROVER/VIEWER) out of their own company's quotes. A non-B2B caller gets
**403 not_b2b** on the request route and **404** on the scoped read/accept/PDF/convert
routes (the quote simply doesn't exist for them). Write actions (request / accept /
convert) require a non-``VIEWER`` role; a ``VIEWER`` is read-only (403).
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_active_user, get_db
from app.crud import company_user as crud_company_user
from app.crud import quote as crud_quote
from app.crud.company_user import CompanyMembership
from app.crud.order import InsufficientStockError, ProductUnavailableError
from app.crud.order import order as crud_order
from app.crud.quote import QuoteNotAcceptableError, QuoteNotConvertibleError, QuoteRequestError
from app.models.company_user import CompanyRole
from app.models.user import User
from app.schemas.order import OrderResponse
from app.schemas.quote import QuoteRequestCreate, QuoteResponse
from app.services import invoice as invoice_service
from app.services import quote_pdf

router = APIRouter()

# B2B roles permitted to take write actions on quotes (request / accept / convert).
# VIEWER is read-only; BUYER / APPROVER / COMPANY_ADMIN may act (a primary contact
# resolves as COMPANY_ADMIN).
_QUOTE_WRITE_ROLES = frozenset(
    {
        CompanyRole.COMPANY_ADMIN.value,
        CompanyRole.BUYER.value,
        CompanyRole.APPROVER.value,
    }
)


def _membership(db: Session, user: User) -> CompanyMembership | None:
    """The caller's active company membership (any active member — not just the
    primary contact), or ``None`` when the caller is not a B2B account."""
    return crud_company_user.get_active_membership(db, user_id=user.id)


def _require_b2b(db: Session, user: User) -> CompanyMembership:
    """Resolve the caller's membership or 403 ``not_b2b``.

    Used by the request route, where "you have no company" is a 403 (you are not a
    B2B account and cannot request quotes). The scoped read/accept/PDF/convert routes
    use a 404 instead (existence not leaked) — they resolve the membership inline.
    """
    membership = _membership(db, user)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not_b2b")
    return membership


def _require_write_role(membership: CompanyMembership) -> None:
    """403 when the member's role is read-only (``VIEWER``) for a write action."""
    if membership.role not in _QUOTE_WRITE_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions")


@router.post("/", response_model=QuoteResponse, status_code=status.HTTP_201_CREATED)
async def request_quote(
    payload: QuoteRequestCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Request a quote from the cart.

    The server prices each line from the current product price in
    ``payload.currency`` (a client-sent price is ignored). 403 ``not_b2b`` if the
    caller is not a member of any company; 403 if the member is a ``VIEWER``
    (read-only); 422 if a line references a missing/inactive product (or ``items``
    is empty — enforced by the schema). status starts ``requested``;
    ``requested_by_user_id`` = caller.
    """
    membership = _require_b2b(db, current_user)
    _require_write_role(membership)
    company = membership.company
    try:
        quote = crud_quote.request_from_cart(
            db,
            obj_in=payload,
            company_id=company.id,
            requested_by_user_id=current_user.id,
        )
    except QuoteRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    db.commit()
    return crud_quote.get_for_company(db, quote_id=quote.id, company_id=company.id)


@router.get("/", response_model=list[QuoteResponse])
async def list_my_quotes(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List the caller's company's quotes (newest first).

    Returns an empty list for a non-B2B caller (no company → no quotes) rather than
    erroring — the FE gates the surface on ``GET /companies/me`` anyway.
    """
    membership = _membership(db, current_user)
    if membership is None:
        return []
    return crud_quote.list_for_company(db, company_id=membership.company.id)


@router.get("/{quote_id}", response_model=QuoteResponse)
async def get_my_quote(
    quote_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get one of the caller's company's quotes. Cross-company/unknown → 404."""
    membership = _membership(db, current_user)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")
    quote = crud_quote.get_for_company(db, quote_id=quote_id, company_id=membership.company.id)
    if quote is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")
    return quote


@router.post("/{quote_id}/accept", response_model=QuoteResponse)
async def accept_my_quote(
    quote_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Accept a ``sent`` quote (→ ``accepted``).

    Only a ``sent`` quote may be accepted (409 otherwise). Cross-company/unknown →
    404. A ``VIEWER`` is read-only (403). Accepting does not create an order —
    convert separately via ``POST /quotes/{id}/convert``.
    """
    membership = _membership(db, current_user)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")
    quote = crud_quote.get_for_company(db, quote_id=quote_id, company_id=membership.company.id)
    if quote is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")
    _require_write_role(membership)
    try:
        crud_quote.accept(db, quote=quote)
    except QuoteNotAcceptableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc
    db.commit()
    return crud_quote.get_for_company(db, quote_id=quote_id, company_id=membership.company.id)


@router.post(
    "/{quote_id}/convert",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def convert_my_quote(
    quote_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Convert one of the caller's company's **accepted** quotes into a pending order.

    Self-service counterpart to the admin convert. The order is owned by the company's
    primary contact (``primary_user_id``) — independent of which member converts — so
    order history, emails and invoices attribute correctly. Line items honour the
    **quoted** unit price + quantity (no re-pricing); stock is decremented from the
    default warehouse like a normal order; ``shipping``/``discount``/``tax`` are 0 and
    no shipping method is set (set at fulfilment).

    Cross-company / unknown quote → 404. A ``VIEWER`` is read-only (403).
    ``status != 'accepted'`` → 422; already converted → 409; no items / any free-text
    line / insufficient stock → 422.
    """
    membership = _membership(db, current_user)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")
    quote = crud_quote.get_for_company(db, quote_id=quote_id, company_id=membership.company.id)
    if quote is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")
    _require_write_role(membership)

    try:
        order = crud_quote.convert_to_order(db, quote=quote, actor_user_id=current_user.id)
    except QuoteNotConvertibleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except ProductUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Product {exc.product_id} is unavailable",
        ) from exc
    except InsufficientStockError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    db.commit()
    return crud_order.get_scoped(db, order_id=order.id, user=current_user)


@router.get(
    "/{quote_id}/quote.pdf",
    responses={200: {"content": {"application/pdf": {}}, "description": "Quote PDF"}},
)
async def download_my_quote_pdf(
    quote_id: int,
    lang: Literal["en", "pl"] = Query("en", description="Quote language"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Download one of the caller's company's quotes as a PDF. Cross-company → 404."""
    membership = _membership(db, current_user)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")
    quote = crud_quote.get_for_company(db, quote_id=quote_id, company_id=membership.company.id)
    if quote is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")

    pdf = quote_pdf.build_quote_pdf(
        quote,
        store=invoice_service.StoreIdentity.from_store_config(db, settings),
        lang=lang,
    )
    filename = f"quote-{quote_pdf.derive_quote_number(quote)}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
