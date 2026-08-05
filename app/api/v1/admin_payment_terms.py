"""Admin B2B payment-terms API — CRUD named net-terms templates.

Admin-gated behind ``CUSTOMERS_WRITE`` (payment terms are a B2B / company
construct, assigned to companies) and audited (resource_type ``payment_term``).
A term is a header: name + type (net_days / due_on_receipt / fixed_date) + the
type's parameter + active flag + notes.

Assignment of a term to a company happens via the **company** admin update
(``PATCH /admin/companies/{id}`` with ``payment_term_id``) — there is no assign
endpoint here. On a PATCH that changes ``term_type``, the matching parameter
(``net_days`` for net_days, ``due_date`` for fixed_date) must be present on the
merged row or the request is 422 — the schema can't check that alone.
"""

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.capabilities import COMPANIES_READ, CUSTOMERS_WRITE
from app.core.dependencies import get_current_admin, get_db, require_capability, require_read
from app.crud import payment_term as crud_payment_term
from app.models.payment_term import PaymentTermType
from app.models.user import User
from app.schemas.payment_term import (
    PaymentTermCreate,
    PaymentTermListResponse,
    PaymentTermResponse,
    PaymentTermUpdate,
)
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

_require = Depends(require_capability(CUSTOMERS_WRITE))
_read = Depends(require_read(COMPANIES_READ))


def _validate_shape(term_type: str, net_days: int | None, due_date) -> None:
    """422 unless the term's parameter matches its (merged) type."""
    if term_type == PaymentTermType.NET_DAYS.value and net_days is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="net_days is required for a net_days term",
        )
    if term_type == PaymentTermType.FIXED_DATE.value and due_date is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="due_date is required for a fixed_date term",
        )


@router.get("/", response_model=PaymentTermListResponse)
async def list_payment_terms(
    db: Session = Depends(get_db),
    current_admin: User = _read,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, description="Case-insensitive substring on name"),
):
    """List payment terms (newest first), searchable by name."""
    skip = (page - 1) * page_size
    rows = crud_payment_term.get_multi(db, search=search, skip=skip, limit=page_size)
    total = crud_payment_term.count(db, search=search)
    pages = ceil(total / page_size) if total > 0 else 1
    return {"items": rows, "total": total, "page": page, "page_size": page_size, "pages": pages}


@router.post(
    "/",
    dependencies=[_require],
    response_model=PaymentTermResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_payment_term(
    payload: PaymentTermCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a payment term. Shape is validated by the schema."""
    row = crud_payment_term.create(db, obj_in=payload)
    audit.record_create(resource_type="payment_term", obj=row)
    db.commit()
    return crud_payment_term.get(db, row.id)


@router.get("/{payment_term_id}", response_model=PaymentTermResponse)
async def get_payment_term(
    payment_term_id: int,
    db: Session = Depends(get_db),
    current_admin: User = _read,
):
    """Get one payment term."""
    row = crud_payment_term.get(db, payment_term_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment term not found")
    return row


@router.patch("/{payment_term_id}", dependencies=[_require], response_model=PaymentTermResponse)
async def update_payment_term(
    payment_term_id: int,
    payload: PaymentTermUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update a payment term (partial). Deactivating it (``is_active`` false)
    makes pay-on-account unavailable for any company assigned it. Validates the
    merged (type, parameter) shape."""
    row = crud_payment_term.get(db, payment_term_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment term not found")
    data = payload.model_dump(exclude_unset=True)
    # term_type is already a str here (use_enum_values on BaseSchema).
    merged_type = data.get("term_type") or row.term_type
    merged_net_days = data.get("net_days", row.net_days)
    merged_due_date = data.get("due_date", row.due_date)
    _validate_shape(merged_type, merged_net_days, merged_due_date)
    crud_payment_term.update(db, db_obj=row, obj_in=payload)
    audit.record_update(resource_type="payment_term", obj=row)
    db.commit()
    return crud_payment_term.get(db, payment_term_id)


@router.delete(
    "/{payment_term_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_payment_term(
    payment_term_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a payment term. Any company assigned it is unassigned
    (``companies.payment_term_id`` → NULL via ON DELETE SET NULL), which turns
    pay-on-account off for that company until a new term is assigned."""
    row = crud_payment_term.get(db, payment_term_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment term not found")
    audit.record_delete(resource_type="payment_term", obj=row)
    crud_payment_term.delete(db, db_obj=row)
    db.commit()
