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

Guest checkout: the three checkout-start endpoints take **optional** auth plus
an optional ``guest_token`` query param. An authed caller resolves their own
order via ``get_scoped`` exactly as before; an anonymous caller must present
the order's ``guest_token`` (constant-time compared; any miss is a plain 404 —
no enumeration signal; neither bearer nor token → 401). Webhooks are untouched
(already unauthenticated + signature-verified).
"""

import json
import logging
import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_db, get_optional_user
from app.crud import order as crud_order
from app.models.order import Order, OrderStatus
from app.models.user import User
from app.schemas.payment import (
    PayUCheckoutResponse,
    StripeCheckoutResponse,
    StripeConfigResponse,
    StripePaymentIntentResponse,
)
from app.services import order_email
from app.services.payu import PayUError, get_payu_client
from app.services.stripe import StripeError, get_stripe_service, stripe_configured

logger = logging.getLogger(__name__)

router = APIRouter()


def _paid_amount_matches(
    order, *, paid_amount_minor: int | None, paid_currency: str | None
) -> bool:
    """Defense-in-depth: a verified webhook's paid amount + currency must equal
    the order's.

    Charge amounts are computed server-side at session/intent creation
    (``round(order.amount_due * 100)`` — the total minus any gift-card /
    store-credit tender), so the buyer can't choose a different amount — this
    is belt-and-suspenders against a tampered/misrouted notification or a
    future flow that lets the client influence the charge. When the provider
    omits the fields we don't block (the signature is the real boundary); a
    present-but-mismatched amount refuses to settle.
    """
    if paid_amount_minor is None or paid_currency is None:
        return True
    expected_minor = round(order.amount_due * 100)
    if paid_amount_minor == expected_minor and paid_currency.upper() == order.currency.upper():
        return True
    logger.warning(
        "Payment amount/currency mismatch for order %s: webhook=%s %s, "
        "expected=%s %s — refusing to settle",
        order.order_number,
        paid_amount_minor,
        paid_currency,
        expected_minor,
        order.currency,
    )
    return False


def _resolve_payable_order(
    db: Session,
    *,
    order_id: int,
    current_user: User | None,
    guest_token: str | None,
) -> Order:
    """Resolve the order a checkout-start call may pay for.

    Authed → owner-scoped lookup exactly as before (admins may start payment
    for any order). Anonymous → the caller must present the order's
    ``guest_token``; it is compared constant-time (``secrets.compare_digest``)
    against the stored token, and any miss — unknown order, authed-user order
    (token NULL), wrong token — is a plain 404, indistinguishable from
    not-found. Neither bearer nor token → 401. Also enforces the shared
    pending-status gate (409 otherwise).
    """
    if current_user is not None:
        order = crud_order.get_scoped(db, order_id=order_id, user=current_user)
    elif guest_token:
        order = crud_order.get(db, id=order_id)
        if order is not None and not (
            order.guest_token is not None and secrets.compare_digest(order.guest_token, guest_token)
        ):
            order = None
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if OrderStatus(order.status) != OrderStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Order is not awaiting payment",
        )
    return order


def _buyer_email(order: Order, current_user: User | None) -> str:
    """Provider-facing buyer email — the account's, or the guest contact."""
    if current_user is not None:
        return current_user.email
    return order.guest_email or ""


def _payment_return_url(order: Order) -> str:
    """Frontend return-page URL for the hosted-redirect flows (PayU continueUrl,
    Stripe Checkout success_url).

    Guest orders additionally carry the track token (``&token=…``) so the
    return page can offer the "view order" CTA — guests have no
    ``/account/orders``, only ``/orders/track/{token}``. The token is
    ``secrets.token_urlsafe`` output (unreserved URL characters only), so no
    extra encoding is needed. Authed orders keep the exact pre-guest URL.
    """
    url = (
        f"{settings.FRONTEND_URL.rstrip('/')}"
        f"/checkout/payment/return?order={order.order_number}"
    )
    if order.user_id is None and order.guest_token:
        url += f"&token={order.guest_token}"
    return url


@router.post("/payu/orders/{order_id}", response_model=PayUCheckoutResponse)
async def create_payu_checkout(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
    guest_token: str | None = Query(default=None, max_length=64),
):
    """Create a PayU payment for the caller's pending order (authed or guest)."""
    # Resolve auth/ownership first so anonymous callers get 401 (not a 503
    # config leak) even when PayU is unconfigured — matches the pre-guest
    # behavior where the auth dependency ran before the route body.
    order = _resolve_payable_order(
        db, order_id=order_id, current_user=current_user, guest_token=guest_token
    )

    client = get_payu_client()  # 503 when PayU is unconfigured

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
            amount_minor=round(order.amount_due * 100),
            currency=order.currency,
            description=f"Order {order.order_number}",
            buyer_email=_buyer_email(order, current_user),
            customer_ip=(request.client.host if request.client else "127.0.0.1"),
            products=products,
            notify_url=f"{settings.BACKEND_URL.rstrip('/')}/api/v1/payments/payu/notify",
            continue_url=_payment_return_url(order),
        )
    except PayUError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider error — please try again.",
        ) from exc

    return {"redirect_uri": result["redirect_uri"]}


@router.post("/payu/notify")
async def payu_notify(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
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
        # PayU sends totalAmount in minor units as a string; currencyCode is ISO.
        raw_total = payu_order.get("totalAmount")
        paid_minor = int(raw_total) if raw_total is not None and str(raw_total).isdigit() else None
        if order is not None and _paid_amount_matches(
            order, paid_amount_minor=paid_minor, paid_currency=payu_order.get("currencyCode")
        ):
            # Email only on the real pending→paid transition (mark_paid no-ops
            # otherwise, so duplicate PayU retries don't re-send).
            was_pending = OrderStatus(order.status) == OrderStatus.PENDING
            crud_order.mark_paid(
                db,
                order=order,
                payment_intent_id=payu_order.get("orderId") or "",
                payment_method="payu",
            )
            db.commit()
            if was_pending:
                background_tasks.add_task(
                    order_email.send_order_event_in_background, order.id, "paid"
                )

    # 200 so PayU stops retrying (we ack even for statuses we don't act on).
    return {"status": "ok"}


@router.post("/stripe/checkout/{order_id}", response_model=StripeCheckoutResponse)
async def create_stripe_checkout(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
    guest_token: str | None = Query(default=None, max_length=64),
):
    """Create a Stripe Checkout Session for the caller's pending order (authed or guest)."""
    # Auth/ownership before the provider-config 503 (see create_payu_checkout).
    order = _resolve_payable_order(
        db, order_id=order_id, current_user=current_user, guest_token=guest_token
    )

    service = get_stripe_service()  # 503 when Stripe is unconfigured

    # A guest has no /account/orders page — cancel back to the token track view.
    if order.user_id is None and order.guest_token:
        cancel_url = f"{settings.FRONTEND_URL.rstrip('/')}/orders/track/{order.guest_token}"
    else:
        cancel_url = f"{settings.FRONTEND_URL.rstrip('/')}/account/orders/{order.id}"

    try:
        # The SDK call is blocking HTTP — run it off the event loop.
        result = await run_in_threadpool(
            service.create_checkout_session,
            ext_order_id=order.order_number,
            amount_minor=round(order.amount_due * 100),
            currency=order.currency,
            description=f"Order {order.order_number}",
            buyer_email=_buyer_email(order, current_user),
            success_url=_payment_return_url(order),
            cancel_url=cancel_url,
        )
    except StripeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider error — please try again.",
        ) from exc

    return {"checkout_url": result["checkout_url"]}


@router.get("/stripe/config", response_model=StripeConfigResponse)
async def stripe_config():
    """Browser-side Stripe config for the in-page Express Checkout Element.

    Deliberately does NOT 503 when Stripe is unconfigured: the frontend reads
    ``configured`` to decide whether to mount the wallet widget at all, so a
    keyless dev/CI env hides it gracefully instead of showing a dead button.
    The publishable key is a browser key by design — safe to return. Public
    (was authed) since guest checkout: a guest's payment-method picker needs
    it to mount the wallet widget, and nothing here is caller-specific.
    """
    return {
        "configured": stripe_configured(),
        "publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
    }


@router.post("/stripe/payment-intent/{order_id}", response_model=StripePaymentIntentResponse)
async def create_stripe_payment_intent(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
    guest_token: str | None = Query(default=None, max_length=64),
):
    """Create a Stripe PaymentIntent for the caller's pending order (wallets).

    Mirrors ``create_stripe_checkout`` (including the optional-auth +
    ``guest_token`` resolution) but returns a ``client_secret`` the browser
    confirms in-page via the Express Checkout Element, instead of a hosted
    redirect URL. Settled server-side by the ``payment_intent.succeeded``
    webhook.
    """
    # Auth/ownership before the provider-config 503 (see create_payu_checkout).
    order = _resolve_payable_order(
        db, order_id=order_id, current_user=current_user, guest_token=guest_token
    )

    service = get_stripe_service()  # 503 when Stripe is unconfigured

    try:
        # The SDK call is blocking HTTP — run it off the event loop.
        result = await run_in_threadpool(
            service.create_payment_intent,
            ext_order_id=order.order_number,
            amount_minor=round(order.amount_due * 100),
            currency=order.currency,
            description=f"Order {order.order_number}",
            buyer_email=_buyer_email(order, current_user),
        )
    except StripeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Payment provider error — please try again.",
        ) from exc

    return {"client_secret": result["client_secret"]}


@router.post("/stripe/webhook")
async def stripe_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
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
    paid_order_id: int | None = None
    if event_type == "checkout.session.completed":
        # payment_status guards against async methods that complete unpaid.
        if obj.get("payment_status") == "paid":
            order_number = obj.get("client_reference_id") or (obj.get("metadata") or {}).get(
                "order_number"
            )
            paid_order_id = _settle_stripe_order(
                db,
                order_number=order_number,
                payment_intent_id=obj.get("payment_intent") or "",
                paid_amount_minor=obj.get("amount_total"),
                paid_currency=obj.get("currency"),
            )
    elif event_type == "payment_intent.succeeded":
        # The event object IS the PaymentIntent; success is implied by the type.
        order_number = (obj.get("metadata") or {}).get("order_number")
        paid_order_id = _settle_stripe_order(
            db,
            order_number=order_number,
            payment_intent_id=obj.get("id") or "",
            paid_amount_minor=obj.get("amount_received") or obj.get("amount"),
            paid_currency=obj.get("currency"),
        )

    if paid_order_id is not None:
        background_tasks.add_task(order_email.send_order_event_in_background, paid_order_id, "paid")

    # 200 so Stripe stops retrying (we ack even for events we don't act on).
    return {"status": "ok"}


def _settle_stripe_order(
    db: Session,
    *,
    order_number: str | None,
    payment_intent_id: str,
    paid_amount_minor: int | None = None,
    paid_currency: str | None = None,
) -> int | None:
    """Flip a pending order to paid from a verified Stripe webhook (idempotent).

    Returns the order id when this call performed the real pending→paid
    transition (so the caller emails exactly once), else None.
    """
    if not order_number:
        return None
    order = crud_order.get_by_order_number(db, order_number=order_number)
    if order is None:
        return None
    if not _paid_amount_matches(
        order, paid_amount_minor=paid_amount_minor, paid_currency=paid_currency
    ):
        return None
    # Idempotent: mark_paid no-ops if the order isn't pending.
    was_pending = OrderStatus(order.status) == OrderStatus.PENDING
    crud_order.mark_paid(
        db, order=order, payment_intent_id=payment_intent_id, payment_method="stripe"
    )
    db.commit()
    return order.id if was_pending else None
