"""Pydantic schemas for admin store-settings management (single-row config).

The store-identity + VAT values are a single row (``id=1``). There is no create
(the migration seeds it) and no delete — only a response shape and a partial
update body. ``invoice_vat_rate`` is constrained 0-100 here AND by a DB CHECK so
the route, schema, and DB stay in lockstep.

All text fields are length-bounded to match the model columns. The update body
fields are all optional (partial PATCH); a field omitted from the body is left
untouched (``exclude_unset`` in the CRUD layer).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from app.schemas.base import BaseSchema, TimestampSchema


class StoreConfigBase(BaseSchema):
    """Shared store-config fields (response shape)."""

    store_name: Annotated[str, Field(max_length=255)]
    address_line: Annotated[str, Field(max_length=255)]
    postal_city: Annotated[str, Field(max_length=255)]
    country: Annotated[str, Field(max_length=255)]
    tax_id: Annotated[str, Field(max_length=64)]
    email: Annotated[str, Field(max_length=255)]
    invoice_vat_rate: Annotated[int, Field(ge=0, le=100)]


class StoreConfigUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/admin/settings`` (all fields optional).

    Only the fields explicitly sent are applied. ``invoice_vat_rate`` is bounded
    0-100 (mirrors the model CHECK constraint).
    """

    store_name: Annotated[str | None, Field(max_length=255)] = None
    address_line: Annotated[str | None, Field(max_length=255)] = None
    postal_city: Annotated[str | None, Field(max_length=255)] = None
    country: Annotated[str | None, Field(max_length=255)] = None
    tax_id: Annotated[str | None, Field(max_length=64)] = None
    email: Annotated[str | None, Field(max_length=255)] = None
    invoice_vat_rate: Annotated[int | None, Field(ge=0, le=100)] = None


class StoreConfigResponse(StoreConfigBase, TimestampSchema):
    """The store-config row as the API returns it (includes ``id`` + timestamps)."""
