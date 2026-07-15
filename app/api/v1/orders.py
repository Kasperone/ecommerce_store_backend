from datetime import date
from math import ceil
from typing import Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from limits import parse as parse_rate_limit
from sqlalchemy.orm import Session

from app.core.capabilities import ORDERS_REFUND, ORDERS_WRITE
from app.core.config import settings
from app.core.dependencies import (
    get_current_active_user,
    get_current_admin,
    get_db,
    get_optional_user,
    require_capability,
)
from app.core.rate_limit import get_ip_key, limiter
from app.crud import invoice as crud_invoice
from app.crud import order as crud_order
from app.crud import order_event as crud_order_event
from app.crud import refund as crud_refund
from app.crud import return_request as crud_return_request
from app.crud import store_config as crud_store_config
from app.crud.coupon import CouponRedemptionError
from app.crud.gift_card import GiftCardError
from app.crud.order import (
    InPostPointRequiredError,
    InsufficientStockError,
    ProductUnavailableError,
    VariantRequiredError,
    VariantUnavailableError,
    status_transition_error,
)
from app.crud.refund import (
    OrderNotRefundableError,
    RefundLineNotOnOrderError,
    RefundLineOverError,
    RefundOverError,
    RestockRequiresItemsError,
)
from app.crud.return_request import (
    OrderNotReturnableError,
    ReturnLineNotOnOrderError,
    ReturnLineOverError,
    ReturnsDisabledError,
    ReturnWindowClosedError,
)
from app.crud.shipping import ShippingRateUnavailableError
from app.crud.store_credit import StoreCreditError
from app.models.order import OrderStatus
from app.models.user import User, UserRole
from app.schemas.order import (
    GuestOrderResponse,
    OrderCreate,
    OrderListResponse,
    OrderResponse,
    OrderUpdate,
    OrderWithUser,
)
from app.schemas.order_event import OrderEventCreate, OrderEventResponse
from app.schemas.refund import RefundCreate, RefundResponse
from app.schemas.return_request import (
    ReturnEligibilityLine,
    ReturnEligibilityResponse,
    ReturnRequestCreate,
    ReturnRequestResponse,
)
from app.services import invoice as invoice_service
from app.services import order_email, order_export, return_email, search_index
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

# Anonymous order creation is an unauthenticated stock decrement — 10/minute
# per IP blunts junk-order stock-burn (accepted v1 posture; no payment-first
# reservation). Enforced manually (not via ``@limiter.limit``) because the
# limit applies only to the anonymous branch — slowapi's ``exempt_when`` takes
# no arguments, so a route decorator would throttle authenticated checkouts too.
_GUEST_CREATE_LIMIT = parse_rate_limit("10/minute")


def _enforce_guest_create_rate_limit(request: Request) -> None:
    """IP-rate-limit anonymous order creation (shares the slowapi storage, so
    the test ``reset_limiter`` fixture and the production Redis backend both
    apply). No-op when rate limiting is disabled (E2E kill-switch)."""
    if not limiter.enabled:
        return
    if not limiter.limiter.hit(_GUEST_CREATE_LIMIT, "guest-order-create", get_ip_key(request)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded: 10 per 1 minute",
        )


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
    request: Request,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
):
    """
    Create an order from posted line items (authenticated or guest).

    Validates availability and stock, snapshots product data, decrements stock,
    and computes pricing. Sets ``status=pending``.

    Auth is **optional**: an anonymous caller must supply ``guest_email``
    (+ optional ``locale``) — 422 otherwise — and is IP-rate-limited at
    10/minute; the order gets a unique ``guest_token`` for account-less
    tracking + payment start. An authenticated caller must NOT send
    ``guest_email`` (422 — the account email is authoritative).
    """
    if current_user is None:
        if order_in.guest_email is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="guest_email is required for guest checkout",
            )
        _enforce_guest_create_rate_limit(request)
    elif order_in.guest_email is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="guest_email is not allowed on an authenticated order",
        )

    # Stored-value tender gating (D7/D8). Toggle-off mirrors the public check
    # endpoint (409 + machine code); store credit is account-bound, so a guest
    # asking for it is a 400 machine code. All ``detail`` values here are
    # machine codes — the FE matches on them, never on prose.
    if order_in.gift_card_code or order_in.use_store_credit:
        config = crud_store_config.get_singleton(db)
        if order_in.gift_card_code and not config.gift_cards_enabled:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="gift_cards_disabled")
        if order_in.use_store_credit and not config.store_credit_enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="store_credit_disabled"
            )
    if order_in.use_store_credit and current_user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="store_credit_requires_account"
        )

    try:
        order = crud_order.create_with_items(
            db, user_id=current_user.id if current_user else None, obj_in=order_in
        )
    except ProductUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Product {exc.product_id} is unavailable",
        ) from exc
    except VariantRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except VariantUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
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
    except InPostPointRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except CouponRedemptionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.message,
        ) from exc
    except GiftCardError as exc:
        # A bad gift-card code fails the order LOUD, like a bad coupon code —
        # never silently ignored. ``detail`` is the machine code, and the
        # statuses mirror the public check endpoint (pinned contract): 404
        # ``gift_card_not_found``; 409 ``gift_card_disabled`` /
        # ``gift_card_expired`` / ``gift_card_depleted`` /
        # ``gift_card_currency_mismatch``.
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
                if exc.code == "gift_card_not_found"
                else status.HTTP_409_CONFLICT
            ),
            detail=exc.code,
        ) from exc
    except StoreCreditError as exc:
        # Only reachable on an under-lock race (store_credit_insufficient) —
        # the create path applies min(balance, remainder) up front. 409 like
        # the gift-card conflict codes.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.code,
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


@router.get("/track/{token}", response_model=GuestOrderResponse)
@limiter.limit("10/minute")
async def track_guest_order(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Track a guest order by its opaque tracking token (public, no account).

    The token (256-bit, from the order-confirmation email / checkout success
    page) is the credential. Returns a trimmed view — status, items, totals,
    shipping method + shipment tracking, city/country — never the full address,
    payment ids, or refund detail. Unknown token → plain 404 (no enumeration
    signal); rate-limited per IP against token scanning.

    NOTE: declared *before* ``GET /{order_id}`` (route-ordering discipline,
    like ``/export``).
    """
    order = crud_order.get_by_guest_token(db, token=token)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return order


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


@router.patch(
    "/{order_id}",
    response_model=OrderWithUser,
    dependencies=[Depends(require_capability(ORDERS_WRITE))],
)
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

    crud_order.apply_admin_update(db, order=order, obj_in=order_in, actor_user_id=current_admin.id)
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
    dependencies=[Depends(require_capability(ORDERS_REFUND))],
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
            destination=refund_in.destination,
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
    except StoreCreditError as exc:
        # destination="store_credit" on a guest order (machine code in detail).
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.code) from exc

    audit.record_create(resource_type="refund", obj=refund)
    # Timeline: refund issued (records the resulting order status + amount so the
    # feed shows partial vs full at a glance). Same txn as the refund.
    crud_order_event.record(
        db,
        order_id=order.id,
        kind="refunded",
        actor_id=current_admin.id,
        payload={
            "amount": str(refund.amount),
            "status": OrderStatus(order.status).value,
            "destination": refund_in.destination,
        },
    )
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


@router.get("/{order_id}/events", response_model=list[OrderEventResponse])
async def list_order_events(
    order_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """The order's timeline, newest first (admin — any staff may read).

    System milestones (created / paid / status_changed / refunded /
    tracking_added / edited) plus free-text staff comments (``kind='note'``).
    404 when the order doesn't exist.
    """
    order = crud_order.get(db, id=order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return crud_order_event.list_for_order(db, order_id=order.id)


@router.post(
    "/{order_id}/events",
    response_model=OrderEventResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_capability(ORDERS_WRITE))],
)
async def create_order_comment(
    order_id: int,
    payload: OrderEventCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Append a free-text staff comment to the order's timeline (``kind='note'``).

    Gated on ``orders:write``. The comment is attributed to the acting staffer.
    """
    order = crud_order.get(db, id=order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    event = crud_order_event.record(
        db,
        order_id=order.id,
        kind="note",
        actor_id=current_admin.id,
        comment=payload.comment,
    )
    db.commit()
    db.refresh(event)
    return event


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


# ---------------------------------------------------------------------------
# Returns (RMA) — customer surface. Owner-scoped via get_scoped (guest orders
# have user_id NULL, so the customer scope never matches them — guests are
# excluded from returns in v1 and use the support email instead).
# ---------------------------------------------------------------------------


@router.get("/{order_id}/returns/eligibility", response_model=ReturnEligibilityResponse)
async def get_return_eligibility(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Check whether (and how much of) an order can still be returned.

    The single server-side source for the storefront return modal: eligible =
    order delivered + returns enabled (``returns_window_days > 0``) + within
    the window of the delivery timestamp (``delivered_at``, legacy fallback
    ``updated_at``) + at least one returnable unit remaining. ``lines`` carries
    the per-line returnable quantity (original minus prior non-rejected
    returns minus unlinked refunds). A non-existent or not-owned order returns
    404 (existence is not leaked).
    """
    order = crud_order.get_scoped(db, order_id=order_id, user=current_user)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    window_days = crud_store_config.get_singleton(db).returns_window_days
    eligible, reason, window_ends_at, returnable = crud_return_request.check_eligibility(
        db, order=order, window_days=window_days
    )
    return ReturnEligibilityResponse(
        eligible=eligible,
        reason=reason,  # pyright: ignore[reportArgumentType] -- CRUD emits only the literal codes
        window_ends_at=window_ends_at,
        lines=[
            ReturnEligibilityLine(
                order_item_id=item.id,
                product_name=item.product_name,
                variant_title=item.variant_title,
                quantity=item.quantity,
                returnable_qty=returnable.get(item.id, 0),
            )
            for item in order.items
        ],
    )


@router.post(
    "/{order_id}/returns",
    response_model=ReturnRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_return_request(
    order_id: int,
    return_in: ReturnRequestCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Request a return for (some of) a delivered order's items.

    Validates the eligibility window (delivered + within
    ``returns_window_days`` of delivery) and the per-line returnable quantity
    (400 on any violation). Creates the request with ``status=requested`` and
    fires the "return requested" email (best-effort). Customer action — no
    audit row (the audit log records admin mutations). A non-existent or
    not-owned order returns 404.
    """
    order = crud_order.get_scoped(db, order_id=order_id, user=current_user)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    window_days = crud_store_config.get_singleton(db).returns_window_days
    try:
        ret = crud_return_request.create_for_order(
            db,
            order=order,
            user_id=current_user.id,
            obj_in=return_in,
            window_days=window_days,
        )
    except OrderNotReturnableError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ReturnsDisabledError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ReturnWindowClosedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ReturnLineNotOnOrderError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ReturnLineOverError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    db.commit()
    # Re-load with the eager options so the response carries the lines + the
    # denormalised order/user fields without lazy round-trips.
    ret = crud_return_request.get(db, return_id=ret.id) or ret
    background_tasks.add_task(return_email.send_return_event_in_background, ret.id, "requested")
    return ret


@router.get("/{order_id}/returns", response_model=list[ReturnRequestResponse])
async def list_order_returns(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    List return requests for an order, scoped to the caller (customer / admin).

    Customers see only their own orders' returns; admins see any. A
    non-existent or not-owned order returns 404 (existence is not leaked). The
    customer surface drops ``admin_note`` client-side — same convention as the
    refund ``note``.
    """
    order = crud_order.get_scoped(db, order_id=order_id, user=current_user)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return crud_return_request.list_for_order(db, order_id=order.id)
