"""Product-tag schemas (the reusable merchandising vocabulary + product chips).

Power ``/api/v1/admin/product-tags`` (vocabulary CRUD) and the ``tags`` chips on
product responses. Mirrors ``app/schemas/tag.py`` (customer tags) with strictly
distinct ``ProductTag`` names so the two systems never collide.
"""

from typing import Annotated

from pydantic import Field, field_validator

from app.schemas.base import BaseSchema, TimestampSchema

PRODUCT_TAG_NAME_MAX = 50
# Hex chip color like "#3b82f6"; null → the frontend picks a neutral default.
HEX_COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"


def _normalize_name(v: object) -> object:
    """Trim + collapse internal whitespace; pass non-strings through untouched
    so type validation still rejects them."""
    return " ".join(v.split()) if isinstance(v, str) else v


class ProductTagCreate(BaseSchema):
    """Create a product tag in the shared vocabulary."""

    name: Annotated[str, Field(min_length=1, max_length=PRODUCT_TAG_NAME_MAX)]
    color: Annotated[str | None, Field(pattern=HEX_COLOR_PATTERN)] = None

    _norm = field_validator("name", mode="before")(_normalize_name)


class ProductTagUpdate(BaseSchema):
    """Rename / recolor a product tag. Both optional (partial PATCH)."""

    name: Annotated[str | None, Field(min_length=1, max_length=PRODUCT_TAG_NAME_MAX)] = None
    color: Annotated[str | None, Field(pattern=HEX_COLOR_PATTERN)] = None

    _norm = field_validator("name", mode="before")(_normalize_name)


class ProductTagChip(BaseSchema):
    """Minimal tag shape for chips attached to a product (no count/timestamps)."""

    id: int
    name: str
    color: str | None = None


class ProductTagResponse(TimestampSchema):
    """A product tag in the vocabulary list, with its product usage count."""

    name: str
    color: str | None = None
    usage_count: int = 0
