"""Admin returns (RMA) API — review, approve/reject/receive, and refund returns.

All routes are admin-gated and audited (resource_type ``return_request``). The
status machine is guarded (``requested → approved | rejected``, ``approved →
received | refunded``, ``received → refunded``; 400 on an illegal move,
mirroring the order ``status_transition_error`` style).

``POST /{id}/refund`` is the only way a return reaches ``refunded``: it
delegates to the existing refund choke point (``crud_refund.create_for_order``)
with the return's lines as the per-line picker, so the running-total / per-line
guards, the KOR, the variant-aware restock movements, and the order-status flip
all come for free — and maps the refund error types to 400 exactly like
``issue_refund`` does. Refunds are allowed from ``received`` (the default flow
— goods physically arrived) AND ``approved`` (ship-back-not-required); the
admin UI copy must stay explicit that **restock happens at refund time**, not
at receive. ``return_requests.refund_id`` is UNIQUE — one refund per return
(a partial-refund-then-second-refund flow for one return is out of scope, v1).
"""

from math import ceil

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.capabilities import ORDERS_REFUND, ORDERS_WRITE
from app.core.dependencies import get_current_admin, get_db, require_capability
from app.core.money import ZERO_MONEY, quantize_money
from app.crud import refund as crud_refund
from app.crud import return_request as crud_return_request
from app.crud.refund import (
    OrderNotRefundableError,
    RefundLineNotOnOrderError,
    RefundLineOverError,
    RefundOverError,
    RestockRequiresItemsError,
)
from app.crud.return_request import REFUNDABLE_RETURN_STATUSES
from app.models.return_request import ReturnRequest, ReturnStatus
from app.models.user import User
from app.schemas.return_request import (
    ReturnActionRequest,
    ReturnRefundRequest,
    ReturnRequestListResponse,
    ReturnRequestResponse,
    ReturnStatusLiteral,
)
from app.services import order_email, return_email, search_index
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

# RMA workflow actions gate on orders:write; the money-moving refund on orders:refund.
_orders_write = Depends(require_capability(ORDERS_WRITE))
_orders_refund = Depends(require_capability(ORDERS_REFUND))


def _get_or_404(db: Session, return_id: int) -> ReturnRequest:
    ret = crud_return_request.get(db, return_id=return_id)
    if ret is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Return request not found"
        )
    return ret


async def _apply_transition_or_400(
    db: Session,
    *,
    ret: ReturnRequest,
    target: ReturnStatus,
    payload: ReturnActionRequest | None,
    audit: AuditWriter,
) -> ReturnRequest:
    """Shared body of the approve / reject / receive routes: guarded transition
    (400 on an illegal move), audit row, commit, eager re-load."""
    error = crud_return_request.transition_error(ret.status, target)
    if error is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)
    crud_return_request.apply_transition(
        db,
        ret=ret,
        target=target,
        admin_note=payload.admin_note if payload is not None else None,
    )
    audit.record_update(resource_type="return_request", obj=ret)
    db.commit()
    return _get_or_404(db, ret.id)


@router.get("/", response_model=ReturnRequestListResponse)
async def list_returns(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    return_status: ReturnStatusLiteral | None = Query(
        None, alias="status", description="Filter by return status"
    ),
):
    """List return requests (newest first), filterable by status."""
    skip = (page - 1) * page_size
    items = crud_return_request.get_multi(db, status=return_status, skip=skip, limit=page_size)
    total = crud_return_request.count(db, status=return_status)
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.get("/{return_id}", response_model=ReturnRequestResponse)
async def get_return(
    return_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Get one return request (lines + denormalised order summary fields)."""
    return _get_or_404(db, return_id)


@router.post(
    "/{return_id}/approve", response_model=ReturnRequestResponse, dependencies=[_orders_write]
)
async def approve_return(
    return_id: int,
    background_tasks: BackgroundTasks,
    payload: ReturnActionRequest | None = None,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Approve a requested return (``requested → approved``; 400 otherwise).

    Fires the "return approved" email (best-effort).
    """
    ret = _get_or_404(db, return_id)
    ret = await _apply_transition_or_400(
        db, ret=ret, target=ReturnStatus.APPROVED, payload=payload, audit=audit
    )
    background_tasks.add_task(return_email.send_return_event_in_background, ret.id, "approved")
    return ret


@router.post(
    "/{return_id}/reject", response_model=ReturnRequestResponse, dependencies=[_orders_write]
)
async def reject_return(
    return_id: int,
    background_tasks: BackgroundTasks,
    payload: ReturnActionRequest | None = None,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Reject a requested return (``requested → rejected``; 400 otherwise).

    Terminal — stamps ``resolved_at``. The optional ``admin_note`` records the
    reason (internal). Fires the "return rejected" email (best-effort).
    """
    ret = _get_or_404(db, return_id)
    ret = await _apply_transition_or_400(
        db, ret=ret, target=ReturnStatus.REJECTED, payload=payload, audit=audit
    )
    background_tasks.add_task(return_email.send_return_event_in_background, ret.id, "rejected")
    return ret


@router.post(
    "/{return_id}/receive", response_model=ReturnRequestResponse, dependencies=[_orders_write]
)
async def receive_return(
    return_id: int,
    payload: ReturnActionRequest | None = None,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Mark an approved return's goods as physically received
    (``approved → received``; 400 otherwise).

    No stock movement here — restock is decided (and written) at refund time
    via ``POST /{id}/refund``, same as the existing refund semantics.
    """
    ret = _get_or_404(db, return_id)
    return await _apply_transition_or_400(
        db, ret=ret, target=ReturnStatus.RECEIVED, payload=payload, audit=audit
    )


@router.post(
    "/{return_id}/refund", response_model=ReturnRequestResponse, dependencies=[_orders_refund]
)
async def refund_return(
    return_id: int,
    payload: ReturnRefundRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Issue the refund for a return and flip it to ``refunded`` (terminal).

    Allowed from ``received`` (default flow) and ``approved``
    (ship-back-not-required); 400 from any other status — including
    ``refunded`` itself, which (with the UNIQUE ``refund_id``) enforces one
    refund per return. ``amount`` defaults to ``Σ order_item.unit_price x
    returned qty``; the items picker is the return's lines, so ``restock=True``
    restocks exactly the returned units (variant-aware via the existing
    ``apply_delta`` loop). Delegates to ``crud_refund.create_for_order`` —
    running-total / per-line guards and the KOR come for free; its error types
    map to 400 exactly like the direct refund endpoint. Fires the existing
    order "refunded" email (KOR attached, best-effort).
    """
    ret = _get_or_404(db, return_id)
    if ReturnStatus(ret.status) not in REFUNDABLE_RETURN_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot refund a return request with status '{ret.status}'. "
                "Refundable statuses: received, approved."
            ),
        )

    items = [(item.order_item_id, item.quantity) for item in ret.items]
    amount = payload.amount
    if amount is None:
        amount = quantize_money(sum((item.line_subtotal or ZERO_MONEY) for item in ret.items))
    order = ret.order

    try:
        refund = crud_refund.create_for_order(
            db,
            order=order,
            amount=amount,
            reason=payload.reason,
            note=payload.note,
            restock=payload.restock,
            actor_user_id=current_admin.id,
            items=items,
        )
    except OrderNotRefundableError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RefundOverError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RefundLineNotOnOrderError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RefundLineOverError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RestockRequiresItemsError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    crud_return_request.mark_refunded(db, ret=ret, refund_id=refund.id)
    audit.record_update(resource_type="return_request", obj=ret)
    db.commit()

    # Stock potentially restored → refresh the in_stock facet (best-effort).
    if payload.restock:
        background_tasks.add_task(
            search_index.sync_products_in_background,
            [item.product_id for item in order.items if item.product_id is not None],
        )
    # Refund-confirmation email with the KOR attached (best-effort) — the
    # existing order "refunded" event, same as the direct refund endpoint.
    background_tasks.add_task(order_email.send_order_event_in_background, order.id, "refunded")
    return _get_or_404(db, ret.id)
