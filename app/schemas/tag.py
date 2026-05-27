"""Customer-tag schemas (the reusable vocabulary + assignment body).

Power ``/api/v1/admin/tags`` (vocabulary CRUD) and the assignment sub-routes
under ``/api/v1/admin/customers/{id}/tags``.
"""

from typing import Annotated

from pydantic import Field, field_validator, model_validator

from app.schemas.base import BaseSchema, TimestampSchema

TAG_NAME_MAX = 50
# Hex chip color like "#3b82f6"; null → the frontend picks a neutral default.
HEX_COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"


def _normalize_name(v: object) -> object:
    """Trim + collapse internal whitespace; pass non-strings through untouched
    so type validation still rejects them."""
    return " ".join(v.split()) if isinstance(v, str) else v


class TagCreate(BaseSchema):
    """Create a tag in the shared vocabulary."""

    name: Annotated[str, Field(min_length=1, max_length=TAG_NAME_MAX)]
    color: Annotated[str | None, Field(pattern=HEX_COLOR_PATTERN)] = None

    _norm = field_validator("name", mode="before")(_normalize_name)


class TagUpdate(BaseSchema):
    """Rename / recolor a tag. Both optional (partial PATCH)."""

    name: Annotated[str | None, Field(min_length=1, max_length=TAG_NAME_MAX)] = None
    color: Annotated[str | None, Field(pattern=HEX_COLOR_PATTERN)] = None

    _norm = field_validator("name", mode="before")(_normalize_name)


class TagChip(BaseSchema):
    """Minimal tag shape for chips attached to a customer (no count/timestamps)."""

    id: int
    name: str
    color: str | None = None


class TagResponse(TimestampSchema):
    """A tag in the vocabulary list, with its customer usage count."""

    name: str
    color: str | None = None
    usage_count: int = 0


class CustomerTagAssign(BaseSchema):
    """Attach a tag to a customer — by ``tag_id`` (existing) or ``name`` (get-or-create).

    Exactly one of the two must be supplied.
    """

    tag_id: int | None = None
    name: Annotated[str | None, Field(min_length=1, max_length=TAG_NAME_MAX)] = None

    _norm = field_validator("name", mode="before")(_normalize_name)

    @model_validator(mode="after")
    def _exactly_one(self) -> "CustomerTagAssign":
        if (self.tag_id is None) == (self.name is None):
            raise ValueError("Provide exactly one of tag_id or name")
        return self
