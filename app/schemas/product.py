from typing import Annotated

from pydantic import Field, computed_field, model_validator

from app.schemas.base import BaseSchema, TimestampSchema
from app.schemas.category import CategoryResponse


class ProductImageResponse(TimestampSchema):
    """One image attached to a product.

    Read-only on this endpoint set — image creation, mutation, and deletion
    happen via the dedicated admin upload endpoints (PR1). ``id``,
    ``created_at``, ``updated_at`` come from ``TimestampSchema``.
    """

    product_id: int
    url: str
    alt_text: str | None = None
    sort_order: int = 0
    is_primary: bool = False
    width: int
    height: int
    bytes: int
    mime: str


class ProductImageUpdate(BaseSchema):
    """Admin-side PATCH body for an existing image row.

    Image bytes are immutable — to change the file, delete and re-upload.
    Promoting to ``is_primary=True`` triggers atomic sibling demotion in
    ``CRUDProductImage.set_primary``.
    """

    alt_text: str | None = None
    sort_order: Annotated[int | None, Field(ge=0)] = None
    is_primary: bool | None = None


class ProductBase(BaseSchema):
    """Base product schema"""

    name: Annotated[str, Field(min_length=1, max_length=200)]
    slug: Annotated[str, Field(min_length=1, max_length=200)]
    description: str | None = None
    sku: Annotated[str | None, Field(max_length=100)] = None

    # Polish locale content — optional, free-text; storefront falls back to the
    # EN field when unset. Mirrors CategoryBase.name_pl/description_pl.
    name_pl: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    description_pl: str | None = None

    # Multi-currency prices — individually optional, at least one required on create.
    # The DB-level CHECK constraint (ck_products_at_least_one_price) enforces the
    # same invariant for raw inserts and bulk loads.
    price_usd: Annotated[float | None, Field(gt=0)] = None
    price_pln: Annotated[float | None, Field(gt=0)] = None
    price_eur: Annotated[float | None, Field(gt=0)] = None

    # Stock can go negative at runtime when ``allow_backorder=True`` and an
    # order is placed for more units than are on hand (see
    # ``crud_inventory.apply_delta`` with ``allow_negative=True``). The
    # response/base shape must surface that real value without crashing
    # serialisation; the ``ge=0`` floor on initial create lives on
    # ``ProductCreate`` below. ``ProductUpdate`` intentionally does not
    # expose ``stock`` — post-create mutations go through the inventory
    # endpoints (adjust / restock), which own the negative-stock rule.
    stock: int = 0
    # Null = no threshold → never flagged "low stock". Editable here and from the
    # admin inventory adjust dialog.
    low_stock_threshold: Annotated[int | None, Field(ge=0)] = None
    # When True, orders may exceed available stock ("on backorder").
    allow_backorder: bool = False
    # Physical weight in kilograms; nullable so legacy products don't block
    # admin creates. The order-create resolver treats NULL as 0 (a product with
    # no weight contributes nothing to the cart's total weight); admins backfill
    # at their pace. Drives weight-tier band matching at checkout.
    weight_kg: Annotated[float | None, Field(ge=0)] = None
    # Physical dimensions in centimetres — all three nullable; legacy products
    # carry NULL. Convention is "all three set or none" — the order-create
    # resolver treats "any one NULL" as "no volumetric contribution" so a
    # half-populated row stays harmless. Drives volumetric-weight pricing
    # alongside ``ShippingZone.dim_divisor``.
    length_cm: Annotated[float | None, Field(ge=0)] = None
    width_cm: Annotated[float | None, Field(ge=0)] = None
    height_cm: Annotated[float | None, Field(ge=0)] = None
    is_active: bool = True
    is_featured: bool = False

    category_id: int | None = None


class ProductCreate(ProductBase):
    """Schema for creating a product"""

    # Initial stock at create time can't be negative. Orders are the only
    # legitimate driver of negative stock and they go through
    # ``crud_inventory.apply_delta``, never through this schema.
    stock: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _at_least_one_currency(self) -> "ProductCreate":
        if self.price_usd is None and self.price_pln is None and self.price_eur is None:
            raise ValueError("At least one price is required (USD, PLN, or EUR)")
        return self


class ProductUpdate(BaseSchema):
    """Schema for updating a product"""

    name: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    slug: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    description: str | None = None
    sku: Annotated[str | None, Field(max_length=100)] = None

    name_pl: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    description_pl: str | None = None

    price_usd: Annotated[float | None, Field(gt=0)] = None
    price_pln: Annotated[float | None, Field(gt=0)] = None
    price_eur: Annotated[float | None, Field(gt=0)] = None

    # `stock` is intentionally NOT updatable here: under multi-warehouse it is the
    # maintained sum of per-warehouse levels, mutated only through the inventory
    # endpoints (adjust / restock / transfer) so the ledger and the
    # Product.stock == sum(levels) invariant stay intact. Set initial stock at
    # create time; change it afterwards via /api/v1/admin/inventory.
    low_stock_threshold: Annotated[int | None, Field(ge=0)] = None
    allow_backorder: bool | None = None
    weight_kg: Annotated[float | None, Field(ge=0)] = None
    length_cm: Annotated[float | None, Field(ge=0)] = None
    width_cm: Annotated[float | None, Field(ge=0)] = None
    height_cm: Annotated[float | None, Field(ge=0)] = None
    is_active: bool | None = None
    is_featured: bool | None = None

    category_id: int | None = None

    @model_validator(mode="after")
    def _at_least_one_currency_if_all_provided(self) -> "ProductUpdate":
        # Only block the "clear all three at once" case. Partial patches that
        # omit prices (e.g., `{"name": "x"}`) leave existing currencies alone
        # and must pass. `model_fields_set` distinguishes "absent from input"
        # from "explicitly null in input" — both look like None on the model
        # after parsing.
        price_fields = {"price_usd", "price_pln", "price_eur"}
        if not price_fields.issubset(self.model_fields_set):
            return self
        if self.price_usd is None and self.price_pln is None and self.price_eur is None:
            raise ValueError("At least one price is required (USD, PLN, or EUR)")
        return self


class ProductResponse(ProductBase, TimestampSchema):
    """Schema for product response"""

    images: list[ProductImageResponse] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def in_stock(self) -> bool:
        """True when stock > 0."""
        return self.stock > 0


class ProductWithCategory(ProductResponse):
    """Product response with category details"""

    category: CategoryResponse | None = None


class ProductListResponse(BaseSchema):
    """Schema for paginated product list"""

    items: list[ProductResponse]
    total: int
    page: int
    page_size: int
    pages: int
