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
from app.schemas.payment import (
    PayUCheckoutResponse,
    StripeCheckoutResponse,
    StripeConfigResponse,
    StripePaymentIntentResponse,
)
from app.services.payu import PayUError, get_payu_client
from app.services.stripe import StripeError, get_stripe_service, stripe_configured

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


@router.get("/stripe/config", response_model=StripeConfigResponse)
async def stripe_config(
    current_user: User = Depends(get_current_active_user),
):
    """Browser-side Stripe config for the in-page Express Checkout Element.

    Deliberately does NOT 503 when Stripe is unconfigured: the frontend reads
    ``configured`` to decide whether to mount the wallet widget at all, so a
    keyless dev/CI env hides it gracefully instead of showing a dead button.
    The publishable key is a browser key by design — safe to return.
    """
    return {
        "configured": stripe_configured(),
        "publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
    }


@router.post("/stripe/payment-intent/{order_id}", response_model=StripePaymentIntentResponse)
async def create_stripe_payment_intent(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Create a Stripe PaymentIntent for the caller's pending order (wallets).

    Mirrors ``create_stripe_checkout`` but returns a ``client_secret`` the
    browser confirms in-page via the Express Checkout Element, instead of a
    hosted redirect URL. Settled server-side by the ``payment_intent.succeeded``
    webhook.
    """
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
            service.create_payment_intent,
            ext_order_id=order.order_number,
            amount_minor=round(order.total * 100),
            currency=order.currency,
            description=f"Order {order.order_number}",
            buyer_email=current_user.email,
        )
    except StripeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider error — please try again.",
        ) from exc

    return {"client_secret": result["client_secret"]}


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
    event_type = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    # Two settle paths, same outcome: the hosted-redirect card button emits
    # checkout.session.completed; the in-page Express Checkout Element (wallets)
    # emits payment_intent.succeeded. Both carry our order_number in metadata.
    if event_type == "checkout.session.completed":
        # payment_status guards against async methods that complete unpaid.
        if obj.get("payment_status") == "paid":
            order_number = obj.get("client_reference_id") or (obj.get("metadata") or {}).get(
                "order_number"
            )
            _settle_stripe_order(
                db, order_number=order_number, payment_intent_id=obj.get("payment_intent") or ""
            )
    elif event_type == "payment_intent.succeeded":
        # The event object IS the PaymentIntent; success is implied by the type.
        order_number = (obj.get("metadata") or {}).get("order_number")
        _settle_stripe_order(db, order_number=order_number, payment_intent_id=obj.get("id") or "")

    # 200 so Stripe stops retrying (we ack even for events we don't act on).
    return {"status": "ok"}


def _settle_stripe_order(db: Session, *, order_number: str | None, payment_intent_id: str) -> None:
    """Flip a pending order to paid from a verified Stripe webhook (idempotent)."""
    if not order_number:
        return
    order = crud_order.get_by_order_number(db, order_number=order_number)
    if order is not None:
        # Idempotent: mark_paid no-ops if the order isn't pending.
        crud_order.mark_paid(
            db, order=order, payment_intent_id=payment_intent_id, payment_method="stripe"
        )
        db.commit()
