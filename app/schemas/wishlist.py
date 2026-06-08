"""Pydantic schemas for the customer wishlist.

``WishlistItemCreate`` is the POST body — a ``product_id`` and an optional
``variant_id`` (a specific option combination of that product). ``WishlistItemRead``
is the list/response shape: it carries enough of a **product card** (slug / name /
primary image / per-currency prices / availability) for the storefront to render a
wishlist tile without a second fetch, plus the variant id / sku / title when the
saved row pins a variant.

Prices are *resolved*: when the row pins a variant, the per-currency prices and
``in_stock`` reflect that variant (a NULL variant price inherits the product's);
otherwise they are the product's own. ``is_active`` is the product's active flag
AND — for a variant row — the variant's, so the UI can grey out a tile whose
product or variant has been retired.
"""

from __future__ import annotations

from app.schemas.base import BaseSchema, TimestampSchema


class WishlistItemCreate(BaseSchema):
    """Request body for ``POST /api/v1/wishlist``."""

    product_id: int
    variant_id: int | None = None


class WishlistItemRead(TimestampSchema):
    """A wishlist row plus the product-card data the storefront renders.

    ``id`` / ``created_at`` / ``updated_at`` come from ``TimestampSchema``. The
    ``product_*`` fields are the card; the ``variant_*`` fields are populated
    only when the row pins a variant.
    """

    product_id: int
    variant_id: int | None = None

    # Product card -----------------------------------------------------------
    product_slug: str
    product_name: str
    product_name_pl: str | None = None
    product_image_url: str | None = None
    # Resolved per-currency prices (variant-effective when a variant is pinned,
    # otherwise the product's own). Individually nullable (a product need not
    # carry every currency).
    price_usd: float | None = None
    price_pln: float | None = None
    price_eur: float | None = None
    # Availability of the saved thing (product, narrowed to the variant if any).
    is_active: bool
    in_stock: bool

    # Variant info (only when variant_id is set) -----------------------------
    variant_sku: str | None = None
    variant_title: str | None = None
