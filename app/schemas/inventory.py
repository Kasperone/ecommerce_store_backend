"""Admin inventory schemas — stock list, manual adjust, and the movement ledger.

These power `/api/v1/admin/inventory`. ``InventoryStatus`` is the computed stock
state (``crud_inventory.status_for``); the manual-adjust endpoint takes an
absolute ``new_stock`` and the route derives the signed delta + records it.
"""

from typing import Annotated, Literal

from pydantic import Field

from app.schemas.base import BaseSchema, TimestampSchema

InventoryStatus = Literal["in_stock", "low_stock", "out_of_stock"]


class InventoryListItem(BaseSchema):
    """One product row in the admin stock list."""

    id: int
    name: str
    sku: str | None = None
    stock: int
    low_stock_threshold: int | None = None
    status: InventoryStatus


class InventoryListResponse(BaseSchema):
    """Paginated admin stock list."""

    items: list[InventoryListItem]
    total: int
    page: int
    page_size: int
    pages: int


class StockAdjustRequest(BaseSchema):
    """Manual stock adjustment body.

    ``new_stock`` is the absolute target; the route computes
    ``delta = new_stock - current`` and rejects a no-op (delta 0). ``note`` is a
    free-text explanation stored on the ledger row (the movement ``reason`` is
    always ``manual_adjustment``).
    """

    new_stock: Annotated[int, Field(ge=0)]
    note: Annotated[str | None, Field(max_length=255)] = None


class InventoryMovementItem(TimestampSchema):
    """One ledger row, with the product name joined in for display."""

    product_id: int
    product_name: str
    delta: int
    resulting_stock: int
    reason: str
    note: str | None = None
    order_id: int | None = None
    actor_user_id: int | None = None


class InventoryMovementListResponse(BaseSchema):
    """Paginated movement ledger."""

    items: list[InventoryMovementItem]
    total: int
    page: int
    page_size: int
    pages: int
