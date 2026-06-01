"""Admin quotes API — create/list/edit/delete B2B price quotes.

All routes are admin-gated and audited (resource_type ``quote``). A quote is a
header (company, currency, validity, notes, status, server-computed totals) plus
nested line items. The **server computes** every ``line_total`` + the
``subtotal`` / ``total`` — they are never read from the request body.

**MVP is manage-only.** Quote→order *conversion* and any customer-facing
request / negotiation flow are deliberately out of scope (deferred). Status
transitions are unconstrained (any → any) — the admin sets the value directly.

Validation: ``status`` / ``currency`` are constrained to their allowed sets by
the schema literals; ``items`` must be non-empty on create (422). The
``company_id`` is checked for existence here (404 if missing).
"""

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_admin, get_db
from app.crud import quote as crud_quote
from app.models.user import User
from app.schemas.quote import (
    QuoteCreate,
    QuoteListResponse,
    QuoteResponse,
    QuoteStatusLiteral,
    QuoteUpdate,
)
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()


@router.get("/", response_model=QuoteListResponse)
async def list_quotes(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: QuoteStatusLiteral | None = Query(
        None, alias="status", description="Filter by quote status"
    ),
    company_id: int | None = Query(None, description="Filter by company id"),
):
    """List quotes (newest first), filterable by status and company.

    Rows carry the company name + totals (no line items — fetch a single quote
    for those)."""
    skip = (page - 1) * page_size
    quotes = crud_quote.get_multi(
        db, status=status_filter, company_id=company_id, skip=skip, limit=page_size
    )
    total = crud_quote.count(db, status=status_filter, company_id=company_id)
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": quotes,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.post("/", response_model=QuoteResponse, status_code=status.HTTP_201_CREATED)
async def create_quote(
    payload: QuoteCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a quote with its line items.

    The server computes each ``line_total`` + the ``subtotal`` / ``total``.
    404s if ``company_id`` does not exist; 422 if ``items`` is empty (enforced by
    the schema). Status defaults to ``draft``."""
    if not crud_quote.company_exists(db, payload.company_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    quote = crud_quote.create_with_items(db, obj_in=payload)
    audit.record_create(resource_type="quote", obj=quote)
    db.commit()
    return crud_quote.get(db, quote.id)


@router.get("/{quote_id}", response_model=QuoteResponse)
async def get_quote(
    quote_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Get one quote by id, with its nested line items."""
    quote = crud_quote.get(db, quote_id)
    if quote is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")
    return quote


@router.patch("/{quote_id}", response_model=QuoteResponse)
async def update_quote(
    quote_id: int,
    payload: QuoteUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update a quote (partial). Only fields sent are applied; ``status`` /
    ``currency`` stay constrained to the allowed sets (422 on an unknown value).
    If ``items`` is provided, the whole line set is replaced and totals
    recomputed; omit ``items`` to leave the lines untouched."""
    quote = crud_quote.get(db, quote_id)
    if quote is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")

    crud_quote.update(db, quote=quote, obj_in=payload)
    audit.record_update(resource_type="quote", obj=quote)
    db.commit()
    return crud_quote.get(db, quote_id)


@router.delete("/{quote_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_quote(
    quote_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a quote (its line items cascade)."""
    quote = crud_quote.get(db, quote_id)
    if quote is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quote not found")
    audit.record_delete(resource_type="quote", obj=quote)
    crud_quote.delete(db, quote=quote)
    db.commit()
