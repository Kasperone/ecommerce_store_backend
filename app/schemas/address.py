"""Pydantic schemas for the user address book.

``AddressCreate`` requires the postal fields (recipient_name / street / city /
postal_code / country); ``label`` / ``state`` / ``phone`` and the two
``is_default_*`` flags are optional. ``AddressUpdate`` is a full replacement
(PUT) — every field optional so a caller can patch a single attribute.

The default-flag mutual-exclusion and first-address-auto-default rules are
enforced in the CRUD layer (transactionally), not here — the schema only carries
the requested flag values.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from app.schemas.base import BaseSchema, TimestampSchema


class AddressBase(BaseSchema):
    """Shared address fields (creation + response)."""

    label: str | None = Field(default=None, max_length=100)
    recipient_name: Annotated[str, Field(min_length=1, max_length=255)]
    street: Annotated[str, Field(min_length=1, max_length=255)]
    city: Annotated[str, Field(min_length=1, max_length=100)]
    postal_code: Annotated[str, Field(min_length=1, max_length=20)]
    country: Annotated[str, Field(min_length=1, max_length=100)]
    state: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=20)
    is_default_shipping: bool = False
    is_default_billing: bool = False


class AddressCreate(AddressBase):
    """Request body for ``POST /api/v1/addresses``."""


class AddressUpdate(BaseSchema):
    """Request body for ``PUT /api/v1/addresses/{id}`` (all fields optional).

    Only fields present in the body change. The ``is_default_*`` flags, when set
    to ``True``, clear the same flag on the caller's other rows (enforced in the
    CRUD layer). Setting a flag ``False`` does not promote another row.
    """

    label: str | None = Field(default=None, max_length=100)
    recipient_name: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    street: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    city: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    postal_code: Annotated[str | None, Field(min_length=1, max_length=20)] = None
    country: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    state: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=20)
    is_default_shipping: bool | None = None
    is_default_billing: bool | None = None


class AddressResponse(AddressBase, TimestampSchema):
    """An address row as the API returns it."""

    user_id: int
