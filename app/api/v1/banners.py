"""Public banners API — the customer-facing read side of storefront banners.

Unauthenticated, like the public product catalog (``products.py``): no
``get_current_*`` dependency. Returns only banners that are **active** AND
currently within their optional display window (``[starts_at, ends_at]``, null
bounds = open-ended on that side), ordered by ``sort_order`` then ``id``.

The admin CRUD lives in ``admin_banners.py`` (gated + audited); this exposes a
slim ``BannerPublic`` shape (no audit/window/``is_active`` noise) for the
storefront renderer. An empty result is a valid 200 ``[]`` — never a 404.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.dependencies import get_db
from app.crud import banner as crud_banner
from app.schemas.banner import BannerPlacementLiteral, BannerPublic

router = APIRouter()


@router.get("/", response_model=list[BannerPublic])
async def list_active_banners(
    db: Session = Depends(get_db),
    placement: BannerPlacementLiteral | None = Query(
        None, description="Optional placement filter; omit for all placements."
    ),
):
    """List active, in-window banners for the storefront (public, no auth)."""
    return crud_banner.get_active(db, placement=placement)
