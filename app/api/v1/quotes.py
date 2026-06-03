"""Customer-facing quotes API (/api/v1/quotes).

Wave C / B2B-1. A B2B customer (a company's primary contact — see D-C-1) can:

- **request** a quote from their cart (``POST /quotes``) — the server prices each
  line from the CURRENT product price in the requested currency (D-C-4, mirrors the
  coupon re-pricing discipline; a client-sent price is never trusted);
- **list / view** their company's quotes (company-scoped — another company's quote
  is a 404, existence not leaked);
- **accept** a ``sent`` quote (``POST /quotes/{id}/accept`` → ``accepted``; any other
  source state → 409, D-C-5);
- **convert** an ``accepted`` quote into a pending order (``POST /quotes/{id}/convert``).
  Self-service for the owning customer (the order is owned by the company's primary
  contact — the caller), mirroring the admin convert: quoted prices honoured, stock
  decremented, ``shipping``/``discount``/``tax`` are 0. Wrong state → 422, already
  converted → 409;
- **download** a quote PDF (``GET /quotes/{id}/quote.pdf?lang=en|pl``).

Every route requires the caller to be a B2B account: their company is resolved via
``companies.primary_user_id == caller.id``. A non-B2B caller gets **403 not_b2b** on
the request route and **404** on the scoped read/accept/PDF routes (the quote simply
doesn't exist for them).
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_active_user, get_db
from app.crud import company as crud_company
from app.crud import quote as crud_quote
from app.crud.order import InsufficientStockError, ProductUnavailableError
from app.crud.order import order as crud_order
from app.crud.quote import QuoteNotAcceptableError, QuoteNotConvertibleError, QuoteRequestError
from app.models.company import Company
from app.models.user import User
from app.schemas.order import OrderResponse
from app.schemas.quote import QuoteRequestCreate, QuoteResponse
from app.services import invoice as invoice_service
from app.services import quote_pdf

router = APIRouter()


def _require_company(db: Session, user: User) -> Company:
    """Resolve the caller's company or 403 ``not_b2b``.

    Used by the request route, where "you have no company" is a 403 (you are not a
    B2B account and cannot request quotes). The scoped read/accept/PDF routes use a
    404 instead (existence not leaked) — they resolve the company inline.
    """
    company = crud_company.get_for_primary_user(db, user.id)
    if company is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not_b2b")
    return company


@router.post("/", response_model=QuoteResponse, status_code=status.HTTP_201_CREATED)
async def request_quote(
    payload: QuoteRequestCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Request a quote from the cart.

    The server prices each line from the current product price in
    ``payload.currency`` (a client-sent price is ignored). 403 ``not_b2b`` if the
    caller is no company's primary contact; 422 if a line references a
    missing/inactive product (or ``items`` is empty — enforced by the schema).
    status starts ``requested``; ``requested_by_user_id`` = caller.
    """
    company = _require_company(db, current_user)
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
    company = crud_company.get_for_primary_user(db, current_user.id)
    if company is None:
        return []
    return crud_quote.list_for_company(db, company_id=company.id)


@router.get("/{quote_id}", response_model=QuoteResponse)
async def get_my_quote(
    quote_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get one of the caller's company's quotes. Cross-company/unknown → 404."""
    company = crud_company.get_for_primary_user(db, current_user.id)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")
    quote = crud_quote.get_for_company(db, quote_id=quote_id, company_id=company.id)
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
    404. Accepting does not create an order — convert separately via
    ``POST /quotes/{id}/convert``.
    """
    company = crud_company.get_for_primary_user(db, current_user.id)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")
    quote = crud_quote.get_for_company(db, quote_id=quote_id, company_id=company.id)
    if quote is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")
    try:
        crud_quote.accept(db, quote=quote)
    except QuoteNotAcceptableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc
    db.commit()
    return crud_quote.get_for_company(db, quote_id=quote_id, company_id=company.id)


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
    primary contact — the caller — so order history, emails and invoices attribute
    correctly. Line items honour the **quoted** unit price + quantity (no re-pricing);
    stock is decremented from the default warehouse like a normal order;
    ``shipping``/``discount``/``tax`` are 0 and no shipping method is set (set at
    fulfilment).

    Cross-company / unknown quote → 404. ``status != 'accepted'`` → 422; already
    converted → 409; no items / any free-text line / insufficient stock → 422.
    """
    company = crud_company.get_for_primary_user(db, current_user.id)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")
    quote = crud_quote.get_for_company(db, quote_id=quote_id, company_id=company.id)
    if quote is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")

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
    company = crud_company.get_for_primary_user(db, current_user.id)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")
    quote = crud_quote.get_for_company(db, quote_id=quote_id, company_id=company.id)
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
