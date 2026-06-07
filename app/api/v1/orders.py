from datetime import date
from math import ceil
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_active_user, get_current_admin, get_db
from app.crud import invoice as crud_invoice
from app.crud import order as crud_order
from app.crud import refund as crud_refund
from app.crud.coupon import CouponRedemptionError
from app.crud.order import (
    InsufficientStockError,
    ProductUnavailableError,
    status_transition_error,
)
from app.crud.refund import (
    OrderNotRefundableError,
    RefundLineNotOnOrderError,
    RefundLineOverError,
    RefundOverError,
    RestockRequiresItemsError,
)
from app.crud.shipping import ShippingRateUnavailableError
from app.models.order import OrderStatus
from app.models.user import User, UserRole
from app.schemas.order import (
    OrderCreate,
    OrderListResponse,
    OrderResponse,
    OrderUpdate,
    OrderWithUser,
)
from app.schemas.refund import RefundCreate, RefundResponse
from app.services import invoice as invoice_service
from app.services import order_email, order_export, search_index
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()


def _touched_product_ids(order) -> list[int]:
    """Product ids on an order's line items (for search re-sync; bulk-sync dedupes)."""
    return [item.product_id for item in order.items if item.product_id is not None]


def _export_filename(
    status: OrderStatus | None, date_from: date | None, date_to: date | None
) -> str:
    """Build a CSV filename reflecting the active filters.

    ``orders`` base, suffixed with the status and/or the date range; falls back
    to ``orders-{today}.csv`` when nothing is filtered.
    """
    parts = ["orders"]
    if status is not None:
        parts.append(OrderStatus(status).value)
    if date_from is not None or date_to is not None:
        parts.append(f"{date_from or 'start'}_{date_to or 'end'}")
    if len(parts) == 1:
        parts.append(date.today().isoformat())
    return "-".join(parts) + ".csv"


@router.post("/", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def create_order(
    order_in: OrderCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Create an order from posted line items (authenticated).

    Validates availability and stock, snapshots product data, decrements stock,
    and computes pricing. Sets ``status=pending``.
    """
    try:
        order = crud_order.create_with_items(db, user_id=current_user.id, obj_in=order_in)
    except ProductUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Product {exc.product_id} is unavailable",
        ) from exc
    except InsufficientStockError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except ShippingRateUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except CouponRedemptionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.message,
        ) from exc

    db.commit()
    db.refresh(order)
    # Stock dropped → refresh the in_stock facet off the hot path (best-effort).
    background_tasks.add_task(search_index.sync_products_in_background, _touched_product_ids(order))
    # Order-confirmation email with the invoice attached (best-effort, off the hot path).
    background_tasks.add_task(order_email.send_order_event_in_background, order.id, "confirmation")
    return order


@router.get("/", response_model=OrderListResponse)
async def list_orders(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    order_status: OrderStatus | None = Query(
        None, alias="status", description="Filter by order status"
    ),
    date_from: date | None = Query(None, description="Orders created on/after this date (UTC)"),
    date_to: date | None = Query(
        None, description="Orders created on/before this date (UTC, inclusive)"
    ),
):
    """
    List orders, scoped to the caller.

    Customers see only their own orders; admins see all. Optional status filter
    and ``created_at`` date range (``date_to`` inclusive, UTC calendar days).
    """
    skip = (page - 1) * page_size
    orders = crud_order.get_multi_scoped(
        db,
        user=current_user,
        status=order_status,
        date_from=date_from,
        date_to=date_to,
        skip=skip,
        limit=page_size,
    )
    total = crud_order.count_scoped(
        db, user=current_user, status=order_status, date_from=date_from, date_to=date_to
    )
    pages = ceil(total / page_size) if total > 0 else 1

    return {
        "items": orders,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.get(
    "/export",
    responses={200: {"content": {"text/csv": {}}, "description": "Orders CSV export"}},
)
async def export_orders(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    order_status: OrderStatus | None = Query(
        None, alias="status", description="Filter by order status"
    ),
    date_from: date | None = Query(None, description="Orders created on/after this date (UTC)"),
    date_to: date | None = Query(
        None, description="Orders created on/before this date (UTC, inclusive)"
    ),
):
    """
    Download orders as a CSV file (admin only).

    Inherits the on-screen filters (status + ``created_at`` date range). One row
    per order with line items packed into the ``items`` column; RFC 4180, UTF-8
    with BOM. Generated on demand from the order snapshot — no persistence and no
    audit row (read-only GET, like the invoice download).

    NOTE: declared *before* ``GET /{order_id}`` so FastAPI does not try to parse
    the literal ``"export"`` as an integer ``order_id`` (which would 422).
    """
    orders = crud_order.get_for_export(
        db,
        user=current_admin,
        status=order_status,
        date_from=date_from,
        date_to=date_to,
    )
    csv_bytes = order_export.build_orders_csv(orders)
    filename = _export_filename(order_status, date_from, date_to)
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{order_id}", response_model=OrderWithUser)
async def get_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Get a single order by ID.

    Customers may only fetch their own orders; admins may fetch any. A
    non-existent or not-owned order returns 404 (existence is not leaked).
    """
    order = crud_order.get_scoped(db, order_id=order_id, user=current_user)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return order


@router.get(
    "/{order_id}/invoice",
    responses={200: {"content": {"application/pdf": {}}, "description": "Invoice PDF"}},
)
async def download_order_invoice(
    order_id: int,
    lang: Literal["en", "pl"] = Query("en", description="Invoice language"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Download an order's invoice as a PDF (generated on demand from the snapshot).

    A paid order yields its numbered faktura (stable number + issue date from the
    registry); a not-yet-paid order yields a proforma. Customers may download only
    their own orders' invoices; admins may download any. A non-existent or not-owned
    order returns 404 (existence is not leaked). Pure read — no number is assigned here.
    """
    order = crud_order.get_scoped(db, order_id=order_id, user=current_user)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    pdf = invoice_service.render_invoice_pdf(
        db,
        order,
        store=invoice_service.StoreIdentity.from_store_config(db, settings),
        lang=lang,
    )
    issued = crud_invoice.get_by_order_id(db, order.id)
    if issued is not None:
        # Series number carries '/'; sanitise for the download filename.
        filename = f"invoice-{issued.invoice_number.replace('/', '-')}.pdf"
    else:
        filename = f"proforma-{invoice_service.derive_proforma_number(order)}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch("/{order_id}", response_model=OrderWithUser)
async def update_order(
    order_id: int,
    order_in: OrderUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """
    Update an order's status (forward workflow) and notes/payment/tracking
    fields (admin).

    Shipment tracking (``shipping_carrier`` / ``tracking_number`` /
    ``tracking_url``) is typically set together with ``status=shipped`` so the
    shipped email carries the tracking link, and may be edited afterwards via a
    tracking-only PATCH. Cancellation and refunds are not done here — use the
    cancel endpoint (refunds are Phase B).
    """
    order = crud_order.get(db, id=order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    previous_status = OrderStatus(order.status).value

    if order_in.status is not None:
        error = status_transition_error(order.status, OrderStatus(order_in.status))
        if error is not None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)

    crud_order.apply_admin_update(db, order=order, obj_in=order_in)
    audit.record_update(resource_type="order", obj=order)
    # Issue the sequential faktura on a genuine admin pending→paid (the webhook path
    # issues via mark_paid). After the audit read: issue_for_order flushes, which
    # would otherwise clear the status change from get_history.
    if OrderStatus(order.status) == OrderStatus.PAID and previous_status != OrderStatus.PAID.value:
        crud_invoice.issue_for_order(db, order=order)
    db.commit()
    db.refresh(order)

    # Notify the customer on a real status change (paid/shipped/delivered).
    new_status = OrderStatus(order.status).value
    if new_status != previous_status:
        event = order_email.event_for_status(new_status)
        if event is not None:
            background_tasks.add_task(order_email.send_order_event_in_background, order.id, event)
    return order


@router.post("/{order_id}/cancel", response_model=OrderResponse)
async def cancel_order(
    order_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Cancel an order and restore stock.

    Customers may cancel only their own order while ``status=pending``. Admins
    may cancel any order that is not already in a terminal state.
    """
    order = crud_order.get_scoped(db, order_id=order_id, user=current_user)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    is_admin = current_user.role == UserRole.ADMIN
    if not is_admin and OrderStatus(order.status) != OrderStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending orders can be cancelled",
        )
    if not crud_order.is_cancellable(order):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel an order with status '{OrderStatus(order.status).value}'",
        )

    crud_order.cancel(db, order=order, actor_user_id=current_user.id)
    db.commit()
    db.refresh(order)
    # Stock restored → refresh the in_stock facet off the hot path (best-effort).
    background_tasks.add_task(search_index.sync_products_in_background, _touched_product_ids(order))
    # Cancellation email (best-effort, off the hot path).
    background_tasks.add_task(order_email.send_order_event_in_background, order.id, "cancelled")
    return order


@router.post(
    "/{order_id}/refunds",
    response_model=RefundResponse,
    status_code=status.HTTP_201_CREATED,
)
async def issue_refund(
    order_id: int,
    refund_in: RefundCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """
    Issue a refund against a paid / processing / shipped / delivered / partially-refunded
    order (admin only).

    v2 accepts partial refunds: ``amount`` is validated against the running
    total (``sum(prior refunds) + amount ≤ order.total``; 400 on over-refund).
    Optional ``items`` records a per-line picker (``order_item_id, quantity``);
    each pick must belong to the order and the cumulative qty across all
    refunds for the same line is capped at ``order_item.quantity``. ``restock=True``
    requires items (400 otherwise) and writes one ``order_refunded`` inventory
    movement per picked line via the ``apply_delta`` choke point — the movement
    quantity is the picked qty, NOT the original line qty.

    Always issues a faktura korygująca **if** the order has a faktura; each
    partial gets its own KOR with its own gap-free number.

    Status flip: cumulative refunds == ``order.total`` (rounding-tolerant) →
    ``refunded``; cumulative > 0 but < total → ``partially_refunded``.

    Forward-only status workflow rules continue to apply: the admin PATCH
    endpoint rejects explicit ``status=refunded`` / ``status=partially_refunded``
    and points the caller here.
    """
    order = crud_order.get_scoped(db, order_id=order_id, user=current_admin)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    item_tuples: list[tuple[int, int]] | None = (
        [(item.order_item_id, item.quantity) for item in refund_in.items]
        if refund_in.items is not None
        else None
    )

    try:
        refund = crud_refund.create_for_order(
            db,
            order=order,
            amount=refund_in.amount,
            reason=refund_in.reason,
            note=refund_in.note,
            restock=refund_in.restock,
            actor_user_id=current_admin.id,
            items=item_tuples,
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

    audit.record_create(resource_type="refund", obj=refund)
    db.commit()
    # Re-load with the eager options so the response carries the KOR + actor +
    # picker without a fresh round-trip (db.refresh doesn't load relationships).
    refund = crud_refund.get(db, refund_id=refund.id) or refund
    # Stock potentially restored → refresh the in_stock facet (best-effort).
    if refund_in.restock:
        background_tasks.add_task(
            search_index.sync_products_in_background, _touched_product_ids(order)
        )
    # Refund-confirmation email with the KOR attached (best-effort, off the hot path).
    background_tasks.add_task(order_email.send_order_event_in_background, order.id, "refunded")
    return refund


@router.get("/{order_id}/refunds", response_model=list[RefundResponse])
async def list_order_refunds(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    List refunds for an order, scoped to the caller (customer / admin).

    Customers see only their own orders' refunds; admins see any. A non-existent
    or not-owned order returns 404 (existence is not leaked). The customer
    surface is expected to drop ``note`` client-side — that field is internal,
    but the schema exposes it because the admin uses the same endpoint shape.
    """
    order = crud_order.get_scoped(db, order_id=order_id, user=current_user)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return crud_refund.list_for_order(db, order_id=order.id)
