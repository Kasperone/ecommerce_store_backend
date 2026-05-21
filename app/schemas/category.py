from typing import Annotated

from pydantic import Field

from app.schemas.base import BaseSchema, TimestampSchema

META_TITLE_MAX = 70
META_DESCRIPTION_MAX = 160


class CategoryBase(BaseSchema):
    """Base category schema (shared by Create and Response).

    All locale + SEO fields are optional — admins fill them in incrementally.
    The DB enforces the same length limits via CHECK constraints; the schema
    mirrors them so a client gets a 422 instead of a 500 from the DB layer.
    """

    name: Annotated[str, Field(min_length=1, max_length=100)]
    slug: Annotated[str, Field(min_length=1, max_length=100)]
    description: str | None = None
    is_active: bool = True
    parent_id: int | None = None
    sort_order: Annotated[int, Field(ge=0)] = 0

    name_pl: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    slug_pl: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    description_pl: str | None = None

    meta_title: Annotated[str | None, Field(max_length=META_TITLE_MAX)] = None
    meta_description: Annotated[str | None, Field(max_length=META_DESCRIPTION_MAX)] = None
    meta_title_pl: Annotated[str | None, Field(max_length=META_TITLE_MAX)] = None
    meta_description_pl: Annotated[str | None, Field(max_length=META_DESCRIPTION_MAX)] = None


class CategoryCreate(CategoryBase):
    """Schema for creating a category"""

    pass


class CategoryUpdate(BaseSchema):
    """Schema for updating a category.

    Mirrors :class:`CategoryBase` but every field is optional so a PATCH can
    touch a single locale without rewriting the whole row.
    """

    name: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    slug: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    description: str | None = None
    is_active: bool | None = None
    parent_id: int | None = None
    sort_order: Annotated[int | None, Field(ge=0)] = None

    name_pl: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    slug_pl: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    description_pl: str | None = None

    meta_title: Annotated[str | None, Field(max_length=META_TITLE_MAX)] = None
    meta_description: Annotated[str | None, Field(max_length=META_DESCRIPTION_MAX)] = None
    meta_title_pl: Annotated[str | None, Field(max_length=META_TITLE_MAX)] = None
    meta_description_pl: Annotated[str | None, Field(max_length=META_DESCRIPTION_MAX)] = None


class CategoryHero(BaseSchema):
    """Hero image columns surfaced on every category response."""

    hero_image_url: str | None = None
    hero_image_alt: str | None = None
    hero_width: int | None = None
    hero_height: int | None = None
    hero_bytes: int | None = None
    hero_mime: str | None = None


class CategoryHeroAltUpdate(BaseSchema):
    """Body of ``PATCH /categories/{id}/hero`` (alt-text only).

    Re-upload uses ``POST /hero``; ``PATCH`` is the cheap metadata path.
    """

    hero_image_alt: Annotated[str | None, Field(max_length=200)] = None


class CategoryHeroResponse(CategoryHero):
    """Returned by the hero upload + alt-text PATCH endpoints."""

    id: int


class CategoryResponse(CategoryBase, CategoryHero, TimestampSchema):
    """Schema for category response"""

    pass


class CategoryWithProductCount(CategoryResponse):
    """Category response with product count"""

    product_count: int = 0


class CategoryTreeNode(CategoryWithProductCount):
    """Recursive tree node for ``GET /categories/tree``.

    Children are pre-sorted by ``(sort_order, id)`` from the CRUD layer.
    """

    children: list["CategoryTreeNode"] = Field(default_factory=list)


class CategoryDeletePreview(BaseSchema):
    """Returned by ``GET /categories/{id}/delete-preview`` so the admin can
    pick between cascade and reparent-to-root before issuing the DELETE."""

    descendant_count: int
    direct_product_count: int
    subtree_product_count: int


class BulkReorderItem(BaseSchema):
    """One row's new position in a bulk reorder request."""

    id: int
    parent_id: int | None = None
    sort_order: Annotated[int, Field(ge=0)]


class BulkReorderRequest(BaseSchema):
    """Body of ``POST /categories/bulk-reorder``.

    Send every row whose position changed. The whole batch is validated
    (depth, cycles, FK targets) before any row is mutated, so a single bad
    item rolls the rest back.
    """

    items: list[BulkReorderItem]
