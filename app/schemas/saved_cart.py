"""Schemas for the saved-cart sync + abandoned-cart recovery endpoints.

The sync body is a best-effort ids+quantities snapshot — product/variant ids
are NOT validated against the catalog on write (stale ids get silently dropped
at recover time). The recovery response carries **fresh** catalog data per
line (``ProductResponse`` / ``ProductVariantPublic``, same shapes as the
public product endpoints) so prices and stock are always current, never a
snapshot.
"""

from typing import Annotated, Literal

from pydantic import Field

from app.schemas.base import BaseSchema
from app.schemas.product import ProductResponse
from app.schemas.product_variant import ProductVariantPublic


class SavedCartItemIn(BaseSchema):
    """One cart line in the sync body (ids + quantity only)."""

    product_id: Annotated[int, Field(ge=1)]
    variant_id: Annotated[int | None, Field(ge=1)] = None
    quantity: Annotated[int, Field(ge=1, le=999)]


class SavedCartSyncRequest(BaseSchema):
    """Body of ``PUT /api/v1/carts/mine``; ``items: []`` deletes the row."""

    items: Annotated[list[SavedCartItemIn], Field(max_length=100)]


class SavedCartSyncResponse(BaseSchema):
    """Ack for the sync upsert; ``item_count`` = sum of quantities (0 = deleted)."""

    status: Literal["ok"] = "ok"
    item_count: int


class RecoveredCartItem(BaseSchema):
    """One recovered line: snapshot quantity + fresh catalog data."""

    quantity: int
    product: ProductResponse
    variant: ProductVariantPublic | None = None


class RecoveredCartResponse(BaseSchema):
    """Body of ``GET /api/v1/carts/recover/{token}``; dead lines are dropped."""

    items: list[RecoveredCartItem] = Field(default_factory=list)
