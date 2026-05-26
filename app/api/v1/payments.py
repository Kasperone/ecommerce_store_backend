"""Payment endpoints — PayU and Stripe, both hosted-redirect flows.

Each provider has a create-checkout endpoint (authenticated; builds a hosted
payment page for the caller's own pending order) and an unauthenticated webhook
the provider calls server-to-server. The webhook signature is the security
boundary in both cases — an unverified body is never trusted — and settling is
idempotent (``crud_order.mark_paid`` no-ops unless the order is still pending).

- ``POST /payments/payu/orders/{order_id}`` → PayU ``redirect_uri``;
  ``POST /payments/payu/notify`` verifies ``OpenPayU-Signature``, settles on ``COMPLETED``.
- ``POST /payments/stripe/checkout/{order_id}`` → Stripe Checkout ``checkout_url``;
  ``POST /payments/stripe/webhook`` verifies ``Stripe-Signature``, settles on a
  ``checkout.session.completed`` event whose ``payment_status == "paid"``.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_active_user, get_db
from app.crud import order as crud_order
from app.models.order import OrderStatus
from app.models.user import User
from app.schemas.payment import PayUCheckoutResponse, StripeCheckoutResponse
from app.services.payu import PayUError, get_payu_client
from app.services.stripe import StripeError, get_stripe_service

router = APIRouter()


@router.post("/payu/orders/{order_id}", response_model=PayUCheckoutResponse)
async def create_payu_checkout(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Create a PayU payment for the caller's pending order."""
    client = get_payu_client()  # 503 when PayU is unconfigured

    order = crud_order.get_scoped(db, order_id=order_id, user=current_user)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if OrderStatus(order.status) != OrderStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order is not awaiting payment",
        )

    products = [
        {
            "name": item.product_name,
            "unitPrice": str(round(item.unit_price * 100)),
            "quantity": str(item.quantity),
        }
        for item in order.items
    ]

    try:
        result = await client.create_order(
            ext_order_id=order.order_number,
            amount_minor=round(order.total * 100),
            currency=order.currency,
            description=f"Order {order.order_number}",
            buyer_email=current_user.email,
            customer_ip=(request.client.host if request.client else "127.0.0.1"),
            products=products,
            notify_url=f"{settings.BACKEND_URL.rstrip('/')}/api/v1/payments/payu/notify",
            continue_url=(
                f"{settings.FRONTEND_URL.rstrip('/')}"
                f"/checkout/payment/return?order={order.order_number}"
            ),
        )
    except PayUError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider error — please try again.",
        ) from exc

    return {"redirect_uri": result["redirect_uri"]}


@router.post("/payu/notify")
async def payu_notify(request: Request, db: Session = Depends(get_db)):
    """PayU server-to-server notification. Verify the signature, then settle."""
    client = get_payu_client()
    raw = await request.body()

    if not client.verify_notification(raw, request.headers.get("OpenPayU-Signature")):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload"
        ) from exc

    payu_order = data.get("order") or {}
    ext_order_id = payu_order.get("extOrderId")
    if ext_order_id and payu_order.get("status") == "COMPLETED":
        order = crud_order.get_by_order_number(db, order_number=ext_order_id)
        if order is not None:
            # Idempotent: mark_paid no-ops if the order isn't pending.
            crud_order.mark_paid(
                db,
                order=order,
                payment_intent_id=payu_order.get("orderId") or "",
                payment_method="payu",
            )
            db.commit()

    # 200 so PayU stops retrying (we ack even for statuses we don't act on).
    return {"status": "ok"}


@router.post("/stripe/checkout/{order_id}", response_model=StripeCheckoutResponse)
async def create_stripe_checkout(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Create a Stripe Checkout Session for the caller's pending order."""
    service = get_stripe_service()  # 503 when Stripe is unconfigured

    order = crud_order.get_scoped(db, order_id=order_id, user=current_user)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if OrderStatus(order.status) != OrderStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order is not awaiting payment",
        )

    try:
        # The SDK call is blocking HTTP — run it off the event loop.
        result = await run_in_threadpool(
            service.create_checkout_session,
            ext_order_id=order.order_number,
            amount_minor=round(order.total * 100),
            currency=order.currency,
            description=f"Order {order.order_number}",
            buyer_email=current_user.email,
            success_url=(
                f"{settings.FRONTEND_URL.rstrip('/')}"
                f"/checkout/payment/return?order={order.order_number}"
            ),
            cancel_url=f"{settings.FRONTEND_URL.rstrip('/')}/account/orders/{order.id}",
        )
    except StripeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider error — please try again.",
        ) from exc

    return {"checkout_url": result["checkout_url"]}


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Stripe server-to-server webhook. Verify the signature, then settle."""
    service = get_stripe_service()
    raw = await request.body()

    # Verify the signature first — the security boundary. On success the raw body
    # is trusted, so we read fields from the parsed JSON as plain dicts (sidesteps
    # the Stripe typed-object attribute quirks; mirrors the PayU webhook). The body
    # is known to be valid JSON here — construct_event already parsed it.
    try:
        service.construct_event(raw, request.headers.get("Stripe-Signature"))
    except StripeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature"
        ) from exc

    event = json.loads(raw)
    if event.get("type") == "checkout.session.completed":
        session = (event.get("data") or {}).get("object") or {}
        # payment_status guards against async methods that complete unpaid.
        if session.get("payment_status") == "paid":
            order_number = session.get("client_reference_id") or (
                session.get("metadata") or {}
            ).get("order_number")
            if order_number:
                order = crud_order.get_by_order_number(db, order_number=order_number)
                if order is not None:
                    crud_order.mark_paid(
                        db,
                        order=order,
                        payment_intent_id=session.get("payment_intent") or "",
                        payment_method="stripe",
                    )
                    db.commit()

    # 200 so Stripe stops retrying (we ack even for events we don't act on).
    return {"status": "ok"}
