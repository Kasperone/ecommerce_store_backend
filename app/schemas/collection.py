"""Collection schemas (manual product collections + membership).

Power ``/api/v1/admin/collections`` (CRUD + hero + membership) and the public
``/api/v1/collections`` read endpoints. Mirror ``app/schemas/category.py`` (the
per-locale + SEO + hero shape) plus the inert ``type`` forward-compat field.
``rules`` (smart-collection predicate tree) stays internal — never exposed for
editing in v1.
"""

from typing import Annotated, Literal

from pydantic import Field

from app.schemas.base import BaseSchema, TimestampSchema

META_TITLE_MAX = 70
META_DESCRIPTION_MAX = 160


class CollectionBase(BaseSchema):
    """Base collection schema (shared by Create and Response).

    All locale + SEO fields are optional — admins fill them in incrementally.
    The DB enforces the same length limits via CHECK constraints; the schema
    mirrors them so a client gets a 422 instead of a 500 from the DB layer.
    """

    name: Annotated[str, Field(min_length=1, max_length=100)]
    slug: Annotated[str, Field(min_length=1, max_length=100)]
    description: str | None = None
    is_active: bool = True
    sort_order: Annotated[int, Field(ge=0)] = 0
    # Inert smart-collection forward-compat. v1 only ever writes 'manual'.
    type: Literal["manual", "smart"] = "manual"

    name_pl: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    slug_pl: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    description_pl: str | None = None

    meta_title: Annotated[str | None, Field(max_length=META_TITLE_MAX)] = None
    meta_description: Annotated[str | None, Field(max_length=META_DESCRIPTION_MAX)] = None
    meta_title_pl: Annotated[str | None, Field(max_length=META_TITLE_MAX)] = None
    meta_description_pl: Annotated[str | None, Field(max_length=META_DESCRIPTION_MAX)] = None


class CollectionCreate(CollectionBase):
    """Schema for creating a collection."""

    pass


class CollectionUpdate(BaseSchema):
    """Schema for updating a collection.

    Mirrors :class:`CollectionBase` but every field is optional so a PATCH can
    touch a single locale without rewriting the whole row.
    """

    name: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    slug: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    description: str | None = None
    is_active: bool | None = None
    sort_order: Annotated[int | None, Field(ge=0)] = None
    type: Literal["manual", "smart"] | None = None

    name_pl: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    slug_pl: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    description_pl: str | None = None

    meta_title: Annotated[str | None, Field(max_length=META_TITLE_MAX)] = None
    meta_description: Annotated[str | None, Field(max_length=META_DESCRIPTION_MAX)] = None
    meta_title_pl: Annotated[str | None, Field(max_length=META_TITLE_MAX)] = None
    meta_description_pl: Annotated[str | None, Field(max_length=META_DESCRIPTION_MAX)] = None


class CollectionHero(BaseSchema):
    """Hero image columns surfaced on every collection response."""

    hero_image_url: str | None = None
    hero_image_alt: str | None = None
    hero_width: int | None = None
    hero_height: int | None = None
    hero_bytes: int | None = None
    hero_mime: str | None = None


class CollectionHeroAltUpdate(BaseSchema):
    """Body of ``PATCH /collections/{id}/hero`` (alt-text only).

    Re-upload uses ``POST /hero``; ``PATCH`` is the cheap metadata path.
    """

    hero_image_alt: Annotated[str | None, Field(max_length=200)] = None


class CollectionHeroResponse(CollectionHero):
    """Returned by the hero upload + alt-text PATCH endpoints."""

    id: int


class CollectionResponse(CollectionBase, CollectionHero, TimestampSchema):
    """Schema for collection response."""

    pass


class CollectionWithProductCount(CollectionResponse):
    """Collection response with member product count."""

    product_count: int = 0


class CollectionProductsSet(BaseSchema):
    """Body of ``PUT /admin/collections/{id}/products``.

    ``product_ids`` is the full membership in display order — the backend
    replaces the join in one transaction, writing ``position`` by array index.
    Passing ``[]`` clears the collection.
    """

    product_ids: list[int] = Field(default_factory=list)
