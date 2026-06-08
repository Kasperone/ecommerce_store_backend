"""Customer wishlist API (`/api/v1/wishlist`).

Owner-scoped: the user is always derived from the bearer token via
``get_current_user`` and every lookup is gated on ``(id, user_id)``, so a caller
can only ever read or mutate their own rows. A cross-user or unknown item id on
delete returns **404** (never 403) — the response is identical whether the row
belongs to someone else or doesn't exist, so it can't be used to probe for other
users' wishlist ids (same anti-enumeration discipline as the address book).

Add is **idempotent**: re-adding the same product (+ variant) returns the
existing row with **200** instead of creating a duplicate or raising 409, which
keeps the storefront "save to wishlist" toggle simple — it never has to
distinguish "already saved" from "just saved". A genuinely new row is **201**.
The ``(user_id, product_id, variant_id)`` UNIQUE constraint is the DB backstop.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.dependencies import get_current_user, get_db
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.user import User
from app.models.wishlist import WishlistItem
from app.schemas.wishlist import WishlistItemCreate, WishlistItemRead

router = APIRouter()


def _primary_image_url(product: Product) -> str | None:
    """The product's primary image url, falling back to the first by sort order."""
    images = product.images
    if not images:
        return None
    for image in images:
        if image.is_primary:
            return image.url
    return images[0].url


def _to_read(item: WishlistItem) -> WishlistItemRead:
    """Build the storefront product-card response for a wishlist row.

    Prices + availability resolve to the pinned variant when present (a NULL
    variant price inherits the product's), otherwise the product's own.
    """
    product = item.product
    variant = item.variant
    if variant is not None:
        price_usd = variant.effective_price_usd
        price_pln = variant.effective_price_pln
        price_eur = variant.effective_price_eur
        in_stock = variant.in_stock
        is_active = product.is_active and variant.is_active
        variant_sku = variant.sku
        variant_title = variant.title
    else:
        price_usd = product.price_usd
        price_pln = product.price_pln
        price_eur = product.price_eur
        in_stock = product.in_stock
        is_active = product.is_active
        variant_sku = None
        variant_title = None

    return WishlistItemRead(
        id=item.id,
        created_at=item.created_at,
        updated_at=item.updated_at,
        product_id=item.product_id,
        variant_id=item.variant_id,
        product_slug=product.slug,
        product_name=product.name,
        product_name_pl=product.name_pl,
        product_image_url=_primary_image_url(product),
        price_usd=price_usd,
        price_pln=price_pln,
        price_eur=price_eur,
        is_active=is_active,
        in_stock=in_stock,
        variant_sku=variant_sku,
        variant_title=variant_title,
    )


def _load_for_user(db: Session, user_id: int) -> list[WishlistItem]:
    """The caller's wishlist rows, newest first, with product/variant eager-loaded."""
    stmt = (
        select(WishlistItem)
        .where(WishlistItem.user_id == user_id)
        .options(
            selectinload(WishlistItem.product).selectinload(Product.images),
            selectinload(WishlistItem.variant),
        )
        .order_by(WishlistItem.created_at.desc(), WishlistItem.id.desc())
    )
    return list(db.execute(stmt).scalars().all())


@router.get("", response_model=list[WishlistItemRead])
async def list_wishlist(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the caller's wishlist items, newest first, as product cards.

    Inactive products/variants are still returned (the row survives until the
    product is deleted); availability is surfaced via ``is_active`` / ``in_stock``
    so the storefront can grey out a retired tile rather than silently drop it.
    """
    return [_to_read(item) for item in _load_for_user(db, current_user.id)]


@router.post("", response_model=WishlistItemRead)
async def add_to_wishlist(
    payload: WishlistItemCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Add a product (optionally a variant) to the caller's wishlist.

    Unknown ``product_id`` → 404. A ``variant_id`` that doesn't exist or doesn't
    belong to ``product_id`` → 404. An exact re-add returns the existing row with
    **200**; a new row is **201**.
    """
    product = db.get(Product, payload.product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    if payload.variant_id is not None:
        variant = db.get(ProductVariant, payload.variant_id)
        if variant is None or variant.product_id != payload.product_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")

    existing = db.execute(
        select(WishlistItem).where(
            WishlistItem.user_id == current_user.id,
            WishlistItem.product_id == payload.product_id,
            WishlistItem.variant_id == payload.variant_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return _to_read(existing)

    item = WishlistItem(
        user_id=current_user.id,
        product_id=payload.product_id,
        variant_id=payload.variant_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    response.status_code = status.HTTP_201_CREATED
    return _to_read(item)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_from_wishlist(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove one of the caller's wishlist items. Unknown/cross-user id → 404."""
    item = db.execute(
        select(WishlistItem).where(
            WishlistItem.id == item_id,
            WishlistItem.user_id == current_user.id,
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wishlist item not found")
    db.delete(item)
    db.commit()
