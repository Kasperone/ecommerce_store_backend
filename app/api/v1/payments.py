"""Payment endpoints (PayU, hosted-redirect flow).

- ``POST /payments/payu/orders/{order_id}`` (authenticated): create a PayU order
  for the caller's own pending order and return PayU's hosted ``redirect_uri``.
- ``POST /payments/payu/notify`` (unauthenticated; PayU calls it): verify the
  ``OpenPayU-Signature`` against the POS second key, then flip the order to
  ``paid`` on a ``COMPLETED`` status. Signature is the security boundary — an
  unverified body is never trusted.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_active_user, get_db
from app.crud import order as crud_order
from app.models.order import OrderStatus
from app.models.user import User
from app.schemas.payment import PayUCheckoutResponse
from app.services.payu import PayUError, get_payu_client

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
            crud_order.mark_paid(db, order=order, payment_intent_id=payu_order.get("orderId") or "")
            db.commit()

    # 200 so PayU stops retrying (we ack even for statuses we don't act on).
    return {"status": "ok"}
