"""Warehouse schemas — stock locations for the multi-warehouse inventory model.

A warehouse is a stock location; per-product quantities live on ``StockLevel``
rows (surfaced via the inventory schemas, not here). Exactly one warehouse is the
**default** — orders are fulfilled from it (v1) — enforced by a partial unique
index and ``crud_warehouse.set_default``.
"""

from typing import Annotated

from pydantic import Field

from app.schemas.base import BaseSchema, TimestampSchema


class WarehouseBase(BaseSchema):
    """Shared warehouse fields."""

    name: Annotated[str, Field(min_length=1, max_length=120)]
    code: Annotated[str, Field(min_length=1, max_length=40)]
    is_active: bool = True
    is_default: bool = False


class WarehouseCreate(WarehouseBase):
    """Request body for creating a warehouse."""


class WarehouseUpdate(BaseSchema):
    """Request body for updating a warehouse (all fields optional)."""

    name: Annotated[str | None, Field(min_length=1, max_length=120)] = None
    code: Annotated[str | None, Field(min_length=1, max_length=40)] = None
    is_active: bool | None = None
    is_default: bool | None = None


class WarehouseResponse(WarehouseBase, TimestampSchema):
    """A warehouse row."""

    id: int


class WarehouseListResponse(BaseSchema):
    """Paginated warehouse list."""

    items: list[WarehouseResponse]
    total: int
    page: int
    page_size: int
    pages: int
