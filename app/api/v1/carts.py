"""Saved-cart sync + abandoned-cart recovery endpoints (`/api/v1/carts`).

Two operations only:

- ``PUT /mine`` (authed) — the frontend's debounced ids+quantities snapshot
  upsert. Empty ``items`` deletes the row (cart emptied / order placed), so
  the detection job never sees a completed checkout.
- ``GET /recover/{token}`` (public, rate-limited) — resolves the opaque email
  token to **fresh** catalog data. The token restores cart contents only, it
  never authenticates, and fetching does not consume it. Mirrors the
  ``orders.py::track_guest_order`` public-token precedent: plain 404 on
  unknown token (no enumeration signal), ``@limiter.limit("10/minute")``
  per IP. Error ``detail`` values are machine codes (``cart_not_found``,
  ``recovery_link_expired``, ``cart_already_ordered``) — the FE matches on
  them, never on prose (draft-orders FE #292 lesson).
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_active_user, get_db
from app.core.rate_limit import limiter
from app.crud import saved_cart as crud_saved_cart
from app.models.product import Product
from app.models.user import User
from app.schemas.saved_cart import (
    RecoveredCartItem,
    RecoveredCartResponse,
    SavedCartSyncRequest,
    SavedCartSyncResponse,
)

router = APIRouter()

# Recovery links die 30 days after the email went out (module constant by
# decision D5 — v1 keeps the config surface to the two agreed store_config keys).
RECOVERY_TOKEN_TTL_DAYS = 30


def _utcnow_naive() -> datetime:
    """Naive UTC now — all datetime columns in this app are naive UTC."""
    return datetime.now(UTC).replace(tzinfo=None)


@router.put("/mine", response_model=SavedCartSyncResponse)
async def sync_my_cart(
    payload: SavedCartSyncRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Upsert the caller's server-side cart snapshot (ids + quantities).

    Best-effort mirror of the browser cart: ids are not validated against the
    catalog here (stale lines get dropped at recover time). ``items: []``
    deletes the row. A deep-equal upsert leaves the row untouched so
    ``updated_at`` keeps measuring real inactivity; any actual change re-arms
    the abandonment candidate (token + sent-at reset). No rate limit — authed,
    cheap, and the frontend debounces.
    """
    items = [item.model_dump() for item in payload.items]
    row = crud_saved_cart.upsert_for_user(db, user_id=current_user.id, items=items)
    db.commit()
    item_count = sum(entry["quantity"] for entry in row.items) if row is not None else 0
    return SavedCartSyncResponse(status="ok", item_count=item_count)


@router.get("/recover/{token}", response_model=RecoveredCartResponse)
@limiter.limit("10/minute")
async def recover_cart(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Resolve a recovery token to fresh cart contents (public, no account).

    The opaque token from the recovery email is the credential. Returns only
    already-public catalog data (product/variant shapes of the public product
    endpoints) plus the snapshot quantities — prices/stock are current, never
    a snapshot. Lines whose product is gone/inactive or whose variant no
    longer exists are silently dropped (``items`` may be empty). Fetching does
    NOT consume the token; it stays valid until the TTL.

    NOTE: keep this route above any future ``/{id}`` route (route-ordering
    discipline — see ``orders.py::track_guest_order``).
    """
    cart = crud_saved_cart.get_by_token(db, token=token)
    if cart is None or cart.recovery_email_sent_at is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="cart_not_found")

    if cart.recovery_email_sent_at < _utcnow_naive() - timedelta(days=RECOVERY_TOKEN_TTL_DAYS):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="recovery_link_expired")

    if crud_saved_cart.has_newer_order(db, user_id=cart.user_id, since=cart.updated_at):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="cart_already_ordered")

    items: list[RecoveredCartItem] = []
    for entry in cart.items or []:
        product = db.get(Product, entry.get("product_id"))
        if product is None or not product.is_active:
            continue
        variant = None
        variant_id = entry.get("variant_id")
        if variant_id is not None:
            variant = next((v for v in product.active_variants if v.id == variant_id), None)
            if variant is None:
                continue
        items.append(
            RecoveredCartItem.model_validate(
                {"quantity": entry.get("quantity", 1), "product": product, "variant": variant}
            )
        )
    return RecoveredCartResponse(items=items)
