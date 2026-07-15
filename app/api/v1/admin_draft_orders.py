"""Admin draft-orders API — compose orders-in-progress before conversion.

All routes are admin-gated and audited (resource_type ``draft_order``). A
draft order is a header (optional customer/guest email, pinned currency,
optional shipping fields, manual discount, notes) plus catalog line items
managed via the ``/items`` sub-endpoints. The **server computes** every
``effective_unit_price`` / ``line_total`` + the ``subtotal`` / ``total`` —
they are never read from the request body.

**Drafts hold NO stock and create NO orders.** Conversion
(``POST /{id}/convert``) is a separate, later endpoint (PR 3). A
``completed`` (converted) draft is immutable — every mutation 409s.

Error posture: missing draft/item/user/shipping-method → 404; catalog
preconditions on a line (inactive product, variant required/unavailable) →
422; mutating a completed draft → 409.
"""

from math import ceil

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.capabilities import ORDERS_WRITE
from app.core.dependencies import get_current_admin, get_db, require_capability
from app.crud import draft_order as crud_draft_order
from app.crud.draft_order import (
    DraftOrderItemError,
    DraftOrderLockedError,
    DraftOrderNotConvertibleError,
)
from app.crud.order import InsufficientStockError, ProductUnavailableError
from app.crud.order import order as crud_order
from app.models.draft_order import DraftOrder, DraftOrderItem
from app.models.user import User
from app.schemas.draft_order import (
    DraftOrderConvert,
    DraftOrderCreate,
    DraftOrderItemCreate,
    DraftOrderItemUpdate,
    DraftOrderListResponse,
    DraftOrderResponse,
    DraftOrderStatusLiteral,
    DraftOrderUpdate,
)
from app.schemas.order import OrderResponse
from app.services import order_email
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

# Capability gate for this router's write/DELETE routes.
_require = Depends(require_capability(ORDERS_WRITE))


def _get_draft_or_404(db: Session, draft_id: int) -> DraftOrder:
    draft = crud_draft_order.get(db, draft_id)
    if draft is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft order not found")
    return draft


def _get_item_or_404(draft: DraftOrder, item_id: int) -> DraftOrderItem:
    for item in draft.items:
        if item.id == item_id:
            return item
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft order item not found")


def _check_user_ref(db: Session, payload: DraftOrderCreate | DraftOrderUpdate) -> None:
    """404 when an explicitly-sent, non-null ``user_id`` references no user."""
    if (
        "user_id" in payload.model_fields_set
        and payload.user_id is not None
        and not crud_draft_order.user_exists(db, payload.user_id)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")


@router.get("/", response_model=DraftOrderListResponse)
async def list_draft_orders(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: DraftOrderStatusLiteral | None = Query(
        None, alias="status", description="Filter by draft status"
    ),
):
    """List draft orders (newest first), filterable by status.

    Rows carry the customer email + totals (no line items — fetch a single
    draft for those)."""
    skip = (page - 1) * page_size
    drafts = crud_draft_order.get_multi(db, status=status_filter, skip=skip, limit=page_size)
    total = crud_draft_order.count(db, status=status_filter)
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": drafts,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.post(
    "/",
    dependencies=[_require],
    response_model=DraftOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_draft_order(
    payload: DraftOrderCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create an empty draft with a pinned ``currency``.

    The customer is optional (``user_id`` and/or ``guest_email`` may be
    omitted while drafting); an explicit ``user_id`` must exist (404). Lines
    are added via ``POST /{id}/items``."""
    _check_user_ref(db, payload)
    draft = crud_draft_order.create(db, obj_in=payload)
    audit.record_create(resource_type="draft_order", obj=draft)
    db.commit()
    return crud_draft_order.get(db, draft.id)


@router.get("/{draft_id}", response_model=DraftOrderResponse)
async def get_draft_order(
    draft_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Get one draft by id, with its nested line items (live-priced)."""
    return _get_draft_or_404(db, draft_id)


@router.patch("/{draft_id}", dependencies=[_require], response_model=DraftOrderResponse)
async def update_draft_order(
    draft_id: int,
    payload: DraftOrderUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update a draft header (partial). Only fields sent are applied; an
    explicit ``null`` clears a nullable field. ``currency`` is immutable
    (pinned at create). A ``discount`` change recomputes the totals. 409 on a
    ``completed`` draft; 404 on an unknown ``user_id`` / ``shipping_method_id``."""
    draft = _get_draft_or_404(db, draft_id)
    _check_user_ref(db, payload)
    if (
        "shipping_method_id" in payload.model_fields_set
        and payload.shipping_method_id is not None
        and not crud_draft_order.shipping_method_exists(db, payload.shipping_method_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Shipping method not found"
        )
    try:
        crud_draft_order.update(db, draft=draft, obj_in=payload)
    except DraftOrderLockedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc
    audit.record_update(resource_type="draft_order", obj=draft)
    db.commit()
    return crud_draft_order.get(db, draft_id)


@router.post(
    "/{draft_id}/convert",
    dependencies=[_require],
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def convert_draft_order(
    draft_id: int,
    payload: DraftOrderConvert,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Convert an **open** draft into a real pending order.

    The order is owned by the draft's customer (``user_id``) or guest
    (``guest_email`` + a fresh ``guest_token``) — the converting admin is only
    the audit actor. Lines honour any ``unit_price_override`` (no re-pricing)
    and otherwise price from the live catalog in the draft's currency; stock
    is validated + decremented from the default warehouse like a normal
    order; shipping resolves for real from the draft's address + method; the
    manual discount is clamped to the subtotal. ``mark_as_paid`` flips the
    order to PAID in the same transaction (issues the gap-free faktura);
    ``send_email`` enqueues the confirmation email (plus the paid email when
    ``mark_as_paid``).

    Eligibility (else error): draft not found → 404; not open / already
    converted → 409; no items, stale (deleted-product) line, no owner,
    incomplete shipping address, no shipping method, variant problems,
    unavailable product, insufficient stock, no rate for the draft's
    country+currency, or an InPost locker method (drafts carry no Paczkomat
    point) → 422.
    """
    draft = _get_draft_or_404(db, draft_id)
    try:
        order = crud_draft_order.convert_to_order(
            db,
            draft=draft,
            payment_method=payload.payment_method,
            mark_as_paid=payload.mark_as_paid,
            actor_user_id=current_admin.id,
        )
    except DraftOrderNotConvertibleError as exc:
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

    audit.record_create(resource_type="order", obj=order)
    audit.record_update(resource_type="draft_order", obj=draft)
    db.commit()
    if payload.send_email:
        # Confirmation (+ paid when marked) off the hot path — best-effort,
        # mirrors the checkout/order routes. Works for guests via guest_email.
        background_tasks.add_task(
            order_email.send_order_event_in_background, order.id, "confirmation"
        )
        if payload.mark_as_paid:
            background_tasks.add_task(order_email.send_order_event_in_background, order.id, "paid")
    # Re-load with items eager-loaded so the 201 body carries the full order view.
    return crud_order.get_scoped(db, order_id=order.id, user=current_admin)


@router.delete("/{draft_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT)
async def delete_draft_order(
    draft_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete an open draft (its line items cascade). 409 on a ``completed``
    draft — that one documents a real conversion."""
    draft = _get_draft_or_404(db, draft_id)
    audit.record_delete(resource_type="draft_order", obj=draft)
    try:
        crud_draft_order.delete(db, draft=draft)
    except DraftOrderLockedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc
    db.commit()


# ---------------------------------------------------------------------------
# Line items — every mutation recomputes the draft totals server-side.
# ---------------------------------------------------------------------------


@router.post(
    "/{draft_id}/items",
    dependencies=[_require],
    response_model=DraftOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_draft_order_item(
    draft_id: int,
    payload: DraftOrderItemCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Add a catalog line to an open draft; returns the updated draft.

    The product must exist and be active, and a product with active variants
    requires a ``variant_id`` (shared order-create rules) — 422 otherwise.
    ``unit_price_override`` pins the line price; omit it for live catalog
    pricing in the draft's currency. No stock is held."""
    draft = _get_draft_or_404(db, draft_id)
    try:
        crud_draft_order.add_item(db, draft=draft, obj_in=payload)
    except DraftOrderLockedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc
    except DraftOrderItemError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message
        ) from exc
    audit.record_update(resource_type="draft_order", obj=draft)
    db.commit()
    return crud_draft_order.get(db, draft_id)


@router.patch(
    "/{draft_id}/items/{item_id}", dependencies=[_require], response_model=DraftOrderResponse
)
async def update_draft_order_item(
    draft_id: int,
    item_id: int,
    payload: DraftOrderItemUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Patch a line's ``quantity`` and/or ``unit_price_override``; returns the
    updated draft. Send an explicit ``unit_price_override: null`` to clear the
    pin back to live catalog pricing. 409 on a ``completed`` draft."""
    draft = _get_draft_or_404(db, draft_id)
    item = _get_item_or_404(draft, item_id)
    try:
        crud_draft_order.update_item(db, draft=draft, item=item, obj_in=payload)
    except DraftOrderLockedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc
    audit.record_update(resource_type="draft_order", obj=draft)
    db.commit()
    return crud_draft_order.get(db, draft_id)


@router.delete(
    "/{draft_id}/items/{item_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT
)
async def remove_draft_order_item(
    draft_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Remove a line from an open draft and recompute the totals. 409 on a
    ``completed`` draft."""
    draft = _get_draft_or_404(db, draft_id)
    item = _get_item_or_404(draft, item_id)
    try:
        crud_draft_order.remove_item(db, draft=draft, item=item)
    except DraftOrderLockedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.message) from exc
    audit.record_update(resource_type="draft_order", obj=draft)
    db.commit()
