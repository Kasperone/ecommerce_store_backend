"""Public gift-card endpoints (`/api/v1/gift-cards`).

One operation: ``POST /check`` — a balance preview for the checkout UI. Public
(gift cards are bearer instruments, D8: guests may redeem) and rate-limited
10/min per IP against code enumeration (D4), mirroring
``carts.py::recover_cart``. Error ``detail`` values are machine codes — the FE
matches on them, never on prose:

- 409 ``gift_cards_disabled`` — the store_config toggle is off (D7)
- 404 ``gift_card_not_found`` — no card for the presented code
- 409 ``gift_card_disabled`` / ``gift_card_expired`` / ``gift_card_depleted``
  / ``gift_card_currency_mismatch`` — card exists but can't pay on this order

The 200 body is deliberately slim (``last4`` + ``balance`` + ``currency``) —
never card metadata like the recipient or notes.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_db
from app.core.rate_limit import limiter
from app.crud import gift_card as crud_gift_card
from app.crud import store_config as crud_store_config
from app.crud.gift_card import GiftCardError
from app.schemas.gift_card import GiftCardCheckRequest, GiftCardCheckResponse

router = APIRouter()


@router.post("/check", response_model=GiftCardCheckResponse)
@limiter.limit("10/minute")
async def check_gift_card(
    payload: GiftCardCheckRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Preview a gift card's usability + balance for a ``currency`` order.

    The presented code is normalized (uppercased, spaces/dashes stripped) and
    looked up by sha256 hash — the plaintext is never stored or logged.
    """
    config = crud_store_config.get_singleton(db)
    if not config.gift_cards_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="gift_cards_disabled")

    card = crud_gift_card.get_by_code(db, code=payload.code)
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="gift_card_not_found")

    try:
        crud_gift_card.check_usable(card=card, currency=payload.currency)
    except GiftCardError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.code) from exc

    return GiftCardCheckResponse(
        last4=card.last4,
        balance=float(card.balance),
        currency=card.currency,
    )
