"""Customer store-credit view (`/api/v1/account/store-credit`).

Owner-scoped like ``account.py``: the user comes off the bearer token, there is
no id in the path. Deliberately **always-on** — the ``store_credit_enabled``
toggle (D7) gates the redemption UX at checkout, not visibility: a customer
whose credit was granted while the program was live must still be able to see
their balance after the toggle goes off. Fresh users simply get empty lists.

Mounted at ``/api/v1/account`` (not ``/me``) per the pinned program contract —
the FE lanes build against ``GET /api/v1/account/store-credit``.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_active_user, get_db
from app.crud import store_credit as crud_store_credit
from app.models.user import User
from app.schemas.store_credit import (
    StoreCreditAccountResponse,
    StoreCreditEntryResponse,
    StoreCreditSummaryResponse,
)

router = APIRouter()


@router.get("/store-credit", response_model=StoreCreditSummaryResponse)
async def get_my_store_credit(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """The caller's per-currency store-credit balances + ledger (newest first)."""
    accounts = crud_store_credit.accounts_for_user(db, user_id=current_user.id)
    entries = crud_store_credit.entries_for_user(db, user_id=current_user.id)
    return StoreCreditSummaryResponse(
        accounts=[StoreCreditAccountResponse.model_validate(a) for a in accounts],
        entries=[
            StoreCreditEntryResponse(
                id=entry.id,
                currency=currency,
                amount=float(entry.amount),
                kind=entry.kind,
                balance_after=float(entry.balance_after),
                order_id=entry.order_id,
                refund_id=entry.refund_id,
                note=entry.note,
                created_at=entry.created_at,
            )
            for entry, currency in entries
        ],
    )
