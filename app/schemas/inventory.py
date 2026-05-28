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
    """Manual stock adjustment body (per warehouse).

    ``new_stock`` is the absolute target for ``warehouse_id``; the route computes
    ``delta = new_stock - <that warehouse's level>`` and rejects a no-op (delta 0).
    ``note`` is a free-text explanation stored on the ledger row (the movement
    ``reason`` is always ``manual_adjustment``).
    """

    warehouse_id: int
    new_stock: Annotated[int, Field(ge=0)]
    note: Annotated[str | None, Field(max_length=255)] = None


class RestockRequest(BaseSchema):
    """Record incoming stock into a warehouse (a purchase-order / supplier receipt).

    Adds ``quantity`` units to ``warehouse_id`` as a ``restock`` movement."""

    warehouse_id: int
    quantity: Annotated[int, Field(ge=1)]
    note: Annotated[str | None, Field(max_length=255)] = None


class TransferRequest(BaseSchema):
    """Move ``quantity`` units of a product from one warehouse to another.

    Writes a paired ``transfer_out`` / ``transfer_in`` movement; the product total
    is unchanged. The source must hold at least ``quantity`` (no backorder on a
    transfer)."""

    product_id: int
    from_warehouse_id: int
    to_warehouse_id: int
    quantity: Annotated[int, Field(ge=1)]
    note: Annotated[str | None, Field(max_length=255)] = None


class TransferResponse(BaseSchema):
    """Result of a stock transfer: the new level in each warehouse."""

    product_id: int
    from_warehouse_id: int
    to_warehouse_id: int
    quantity: int
    from_resulting_stock: int
    to_resulting_stock: int


class StockLevelItem(BaseSchema):
    """One per-warehouse stock level for a product (the breakdown view).

    A product's default warehouse always appears (synthesised from unallocated
    stock when no level row has been materialised yet)."""

    warehouse_id: int
    warehouse_code: str
    warehouse_name: str
    quantity: int
    is_default: bool


class InventoryMovementItem(TimestampSchema):
    """One ledger row, with product + warehouse names joined in for display."""

    product_id: int
    product_name: str
    warehouse_id: int | None = None
    warehouse_name: str | None = None
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
