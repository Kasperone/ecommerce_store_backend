from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, computed_field, model_validator

from app.schemas.base import BaseSchema, TimestampSchema
from app.schemas.category import CategoryResponse
from app.schemas.product_tag import ProductTagChip
from app.schemas.product_variant import ProductOptionResponse, ProductVariantPublic

# Cap on the number of products a single bulk-edit may touch — keeps the
# one-transaction batch bounded (mirrors the ~5k CSV import cap).
BULK_EDIT_MAX_IDS = 1000

# Required display name: trim surrounding whitespace and reject blank-only
# input. ``Field(min_length=1)`` alone accepts ``"   "`` (length 3); with
# ``strip_whitespace`` the min/max length validate the *trimmed* value, so a
# whitespace-only name 422s and a padded name is stored trimmed. OpenAPI output
# is unchanged (strip_whitespace is a parse step, not a schema constraint).
ProductName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


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

    name: ProductName
    slug: Annotated[str, Field(min_length=1, max_length=200)]
    description: str | None = None
    sku: Annotated[str | None, Field(max_length=100)] = None

    # Polish locale content — optional, free-text; storefront falls back to the
    # EN field when unset. Mirrors CategoryBase.name_pl/description_pl.
    name_pl: ProductName | None = None
    description_pl: str | None = None

    # Merchant identity (SEO blueprint D11) — feeds PDP JSON-LD + merchant
    # feeds. All optional; ``brand`` falls back to the store Organization in
    # JSON-LD, ``condition`` defaults to 'new'.
    brand: Annotated[str | None, Field(max_length=100)] = None
    gtin: Annotated[str | None, Field(max_length=14)] = None
    mpn: Annotated[str | None, Field(max_length=70)] = None
    condition: Literal["new", "refurbished", "used"] = "new"
    google_product_category: Annotated[
        str | None,
        Field(max_length=20, description="Google product taxonomy numeric ID, e.g. '5613'"),
    ] = None

    # SEO meta (per locale) — mirrors CategoryBase; PDP falls back to
    # name / description when unset. Lengths match the DB CHECKs (70/160).
    meta_title: Annotated[str | None, Field(max_length=70)] = None
    meta_description: Annotated[str | None, Field(max_length=160)] = None
    meta_title_pl: Annotated[str | None, Field(max_length=70)] = None
    meta_description_pl: Annotated[str | None, Field(max_length=160)] = None

    # Multi-currency prices — individually optional, at least one required on create.
    # The DB-level CHECK constraint (ck_products_at_least_one_price) enforces the
    # same invariant for raw inserts and bulk loads.
    price_usd: Annotated[float | None, Field(gt=0)] = None
    price_pln: Annotated[float | None, Field(gt=0)] = None
    price_eur: Annotated[float | None, Field(gt=0)] = None
    # Polish VAT rate (%) — 23 standard, 8/5/0 for reduced-rate goods. Drives
    # per-line faktura VAT decomposition; defaults to standard.
    vat_rate: Annotated[int, Field(ge=0, le=100)] = 23

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

    # Merchandising tags by name — free-form get-or-create on the product form
    # (the Shopify "type tags on the product" model). The backend trims/collapses
    # whitespace, case-insensitively get-or-creates the vocabulary rows, and syncs
    # the join. ``None`` = no tags on create.
    tags: list[str] | None = None

    @model_validator(mode="after")
    def _at_least_one_currency(self) -> "ProductCreate":
        if self.price_usd is None and self.price_pln is None and self.price_eur is None:
            raise ValueError("At least one price is required (USD, PLN, or EUR)")
        return self


class ProductUpdate(BaseSchema):
    """Schema for updating a product"""

    name: ProductName | None = None
    slug: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    description: str | None = None
    sku: Annotated[str | None, Field(max_length=100)] = None

    name_pl: ProductName | None = None
    description_pl: str | None = None

    brand: Annotated[str | None, Field(max_length=100)] = None
    gtin: Annotated[str | None, Field(max_length=14)] = None
    mpn: Annotated[str | None, Field(max_length=70)] = None
    condition: Literal["new", "refurbished", "used"] | None = None
    google_product_category: Annotated[str | None, Field(max_length=20)] = None

    meta_title: Annotated[str | None, Field(max_length=70)] = None
    meta_description: Annotated[str | None, Field(max_length=160)] = None
    meta_title_pl: Annotated[str | None, Field(max_length=70)] = None
    meta_description_pl: Annotated[str | None, Field(max_length=160)] = None

    price_usd: Annotated[float | None, Field(gt=0)] = None
    price_pln: Annotated[float | None, Field(gt=0)] = None
    price_eur: Annotated[float | None, Field(gt=0)] = None
    vat_rate: Annotated[int | None, Field(ge=0, le=100)] = None

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

    # Merchandising tags by name. ``None`` (omitted) = leave the product's tags
    # unchanged; ``[]`` = clear all tags; a list = replace with the get-or-create
    # vocabulary rows for those names.
    tags: list[str] | None = None

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


class ProductBulkSet(BaseSchema):
    """The narrow set of fields a bulk-edit may write across many products.

    Deliberately a small subset of ``ProductUpdate``: only price (per currency),
    ``is_active``, and ``low_stock_threshold``. **Stock is intentionally absent** —
    stock is the maintained sum of per-warehouse levels and may only move through
    the inventory ledger (``apply_delta``), never a direct column write. Adding a
    ``stock`` key here is rejected by ``extra="forbid"``.

    Every field is optional; the route requires at least one to be present.
    Prices follow the catalog rule (``> 0``); ``low_stock_threshold`` is ``>= 0``
    or ``None`` (None = clear the per-product threshold → never flagged low-stock).
    """

    model_config = ConfigDict(extra="forbid")

    price_usd: Annotated[float | None, Field(gt=0)] = None
    price_pln: Annotated[float | None, Field(gt=0)] = None
    price_eur: Annotated[float | None, Field(gt=0)] = None
    is_active: bool | None = None
    low_stock_threshold: Annotated[int | None, Field(ge=0)] = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "ProductBulkSet":
        if not self.model_fields_set:
            raise ValueError("At least one field is required in 'set'")
        return self


class ProductBulkUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/admin/products/bulk``.

    Applies ``set`` to every product in ``product_ids`` in one transaction. The id
    list must be non-empty and is capped to keep the single-transaction batch
    bounded. Any id that doesn't resolve to a product fails the whole request
    (all-or-nothing — see the route).
    """

    product_ids: Annotated[list[int], Field(min_length=1, max_length=BULK_EDIT_MAX_IDS)]
    set: ProductBulkSet

    @model_validator(mode="after")
    def _unique_ids(self) -> "ProductBulkUpdate":
        # De-dup is the route's job, but reject obviously-malformed input early.
        if len(set(self.product_ids)) != len(self.product_ids):
            raise ValueError("product_ids must not contain duplicates")
        return self


class ProductBulkUpdateResult(BaseSchema):
    """Summary returned by a successful bulk-edit."""

    updated: int
    product_ids: list[int]


class ProductResponse(ProductBase, TimestampSchema):
    """Schema for product response"""

    images: list[ProductImageResponse] = Field(default_factory=list)
    # Merchandising tag chips (the product's ``tags`` relationship). Additive:
    # defaults to ``[]`` so untagged products are byte-for-byte unchanged. List
    # endpoints eager-load ``Product.tags`` so this doesn't N+1.
    tags: list[ProductTagChip] = Field(default_factory=list)
    # True when the product has at least one active variant (read from the
    # model's ``has_variants`` property). Additive: variantless products keep
    # today's shape with ``has_variants=False``. List endpoints eager-load
    # ``Product.variants`` so this doesn't N+1.
    has_variants: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def in_stock(self) -> bool:
        """True when stock > 0."""
        return self.stock > 0


class ProductWithCategory(ProductResponse):
    """Product response with category details (the detail endpoints).

    Carries the option/variant matrix for the PDP: ``options`` with their
    values, and **active** variants only (``variants`` reads the model's
    ``active_variants`` property via its validation alias). Both default to
    empty lists — variantless products are byte-for-byte unchanged apart from
    the additive keys.
    """

    category: CategoryResponse | None = None
    options: list[ProductOptionResponse] = Field(default_factory=list)
    variants: list[ProductVariantPublic] = Field(
        default_factory=list, validation_alias="active_variants"
    )


class ProductListResponse(BaseSchema):
    """Schema for paginated product list"""

    items: list[ProductResponse]
    total: int
    page: int
    page_size: int
    pages: int
