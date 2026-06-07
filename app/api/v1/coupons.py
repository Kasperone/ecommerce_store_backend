"""Customer-facing coupon endpoint — validate/preview a code at checkout.

Distinct from ``app/api/v1/admin_coupons.py`` (the admin management CRUD). This
single route lets the storefront preview a discount before the order is placed.
It is **advisory**: the authoritative validation + application happens in the
order-create path (``crud_order.create_with_items`` → ``crud_coupon.redeem``), so
a stale or tampered preview can never let an invalid code reduce a real total.

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
    """Preview a coupon's discount against the posted subtotal + currency.

    200 with the computed discount when eligible; 400 with the failure reason
    (expired / inactive / min-order-not-met / currency-mismatch / usage-exhausted
    / per-user-exhausted / not-found) otherwise. No state is mutated —
    ``used_count`` is bumped only at order create. An authenticated user is
    passed through so the preview reflects the per-user cap; an anonymous
    (guest-checkout) preview skips that one check.
    """
    try:
        coupon, discount = crud_coupon.validate_for_redemption(
            db,
            code=payload.code,
            subtotal=payload.subtotal,
            currency=payload.currency,
            user_id=current_user.id if current_user else None,
        )
    except CouponRedemptionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc

    return CouponValidateResponse(
        code=coupon.code,
        discount_type=coupon.discount_type,  # type: ignore[arg-type]
        discount_amount=discount,
        currency=payload.currency,
    )
