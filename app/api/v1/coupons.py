"""Customer-facing coupon endpoint — preview the discount checkout would apply.

Distinct from ``app/api/v1/admin_coupons.py`` (the admin management CRUD). This
single route lets the storefront preview a discount before the order is placed
— a typed code, or (with no code) any applicable **automatic** discount. It
routes through the same picker as the order-create path
(``crud_coupon.pick_best``), so the preview can never diverge from what checkout
actually applies. It is **advisory**: the authoritative validation + application
happens in ``crud_order.create_with_items`` → ``crud_coupon.redeem``, so a stale
or tampered preview can never let an invalid code reduce a real total.

Optional auth (guest checkout previews coupons too) and rate-limited per IP so
the endpoint can't be used to brute-force valid codes. An anonymous preview
skips the per-user cap (``user_id=None``) — same as the guest order-create
path, where ``per_user_limit`` is unenforceable (documented limitation).
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_db, get_optional_user
from app.core.rate_limit import limiter
from app.crud import coupon as crud_coupon
from app.crud.coupon import CouponRedemptionError
from app.models.user import User
from app.schemas.coupon import CouponValidateRequest, CouponValidateResponse

router = APIRouter()


@router.post("/validate", response_model=CouponValidateResponse)
@limiter.limit("20/minute")
async def validate_coupon(
    request: Request,
    payload: CouponValidateRequest,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
):
    """Preview the discount checkout would apply (typed code or automatic).

    With a ``code``: 200 with the winning discount when the code is eligible
    (an eligible automatic with a larger saving may win instead — best single
    discount, exactly as checkout decides); 400 with the failure reason
    (expired / inactive / min-order-not-met / currency-mismatch /
    usage-exhausted / per-user-exhausted / not-found) when the code itself is
    invalid — a bad code never silently falls back to an automatic.

    Without a ``code``: 200 with the best applicable automatic discount, or a
    200 with NULL applied-fields when nothing applies.

    ``shipping_amount`` is what a free_shipping coupon saves (send the
    already-resolved amount, post free-above-threshold). No state is mutated —
    ``used_count`` is bumped only at order create. An authenticated user is
    passed through so the preview reflects the per-user cap; an anonymous
    (guest-checkout) preview skips that one check.
    """
    try:
        applied = crud_coupon.pick_best(
            db,
            code=payload.code,
            subtotal=payload.subtotal,
            shipping=payload.shipping_amount,
            currency=payload.currency,
            user_id=current_user.id if current_user else None,
        )
    except CouponRedemptionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc

    if applied is None:
        return CouponValidateResponse(
            code=None,
            discount_type=None,
            discount_amount=None,
            is_automatic=False,
            free_shipping=False,
            currency=payload.currency,
        )

    return CouponValidateResponse(
        code=applied.coupon.code,
        discount_type=applied.coupon.discount_type,  # type: ignore[arg-type]
        discount_amount=applied.saving,
        is_automatic=applied.coupon.is_automatic,
        free_shipping=applied.free_shipping,
        currency=payload.currency,
    )
