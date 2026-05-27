from math import ceil
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_active_user, get_current_admin, get_db
from app.crud import order as crud_order
from app.crud.order import (
    InsufficientStockError,
    ProductUnavailableError,
    status_transition_error,
)
from app.models.order import OrderStatus
from app.models.user import User, UserRole
from app.schemas.order import (
    OrderCreate,
    OrderListResponse,
    OrderResponse,
    OrderUpdate,
    OrderWithUser,
)
from app.services import invoice as invoice_service
from app.services import order_email, search_index
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()


def _touched_product_ids(order) -> list[int]:
    """Product ids on an order's line items (for search re-sync; bulk-sync dedupes)."""
    return [item.product_id for item in order.items if item.product_id is not None]


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
):
    """
    List orders, scoped to the caller.

    Customers see only their own orders; admins see all. Optional status filter.
    """
    skip = (page - 1) * page_size
    orders = crud_order.get_multi_scoped(
        db, user=current_user, status=order_status, skip=skip, limit=page_size
    )
    total = crud_order.count_scoped(db, user=current_user, status=order_status)
    pages = ceil(total / page_size) if total > 0 else 1

    return {
        "items": orders,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


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

    Customers may download only their own orders' invoices; admins may download
    any. A non-existent or not-owned order returns 404 (existence is not leaked).
    """
    order = crud_order.get_scoped(db, order_id=order_id, user=current_user)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    pdf = invoice_service.build_invoice_pdf(
        order,
        store=invoice_service.StoreIdentity.from_settings(settings),
        lang=lang,
    )
    filename = f"invoice-{order.order_number}.pdf"
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
    Update an order's status (forward workflow) and notes/payment fields (admin).

    Cancellation and refunds are not done here — use the cancel endpoint
    (refunds are Phase B).
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
