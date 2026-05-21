from typing import Annotated

from pydantic import Field

from app.schemas.base import BaseSchema, TimestampSchema


class CategoryBase(BaseSchema):
    """Base category schema"""

    name: Annotated[str, Field(min_length=1, max_length=100)]
    slug: Annotated[str, Field(min_length=1, max_length=100)]
    description: str | None = None
    is_active: bool = True
    parent_id: int | None = None
    sort_order: Annotated[int, Field(ge=0)] = 0


class CategoryCreate(CategoryBase):
    """Schema for creating a category"""

    pass


class CategoryUpdate(BaseSchema):
    """Schema for updating a category"""

    name: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    slug: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    description: str | None = None
    is_active: bool | None = None
    parent_id: int | None = None
    sort_order: Annotated[int | None, Field(ge=0)] = None


class CategoryResponse(CategoryBase, TimestampSchema):
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
