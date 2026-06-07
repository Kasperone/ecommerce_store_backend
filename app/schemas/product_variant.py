"""Product option/variant schemas — the admin matrix write + public/admin reads.

The matrix PUT (``PUT /api/v1/products/{id}/options``) takes the full desired
state (options with values + the variant rows) and the backend reconciles in
one transaction. Caps are enforced here at the Pydantic boundary (≤3 options,
≤100 variants → automatic 422) and re-checked structurally in the CRUD layer.

``ProductVariantPublic`` exposes **effective** per-currency prices (a NULL
variant price inherits the product's) via the model's ``effective_price_*``
properties; the admin ``ProductVariantResponse`` exposes the raw columns so
the editor can tell "override" from "inherit".
"""

from typing import Annotated

from pydantic import Field, model_validator

from app.schemas.base import BaseSchema, TimestampSchema


class ProductOptionValueResponse(TimestampSchema):
    """One selectable value of an option (e.g. "Red")."""

    option_id: int
    value: str
    value_pl: str | None = None
    position: int = 0


class ProductOptionResponse(TimestampSchema):
    """One option axis of a product (e.g. "Color"), with its ordered values."""

    product_id: int
    name: str
    name_pl: str | None = None
    position: int = 0
    values: list[ProductOptionValueResponse] = Field(default_factory=list)


class ProductVariantResponse(TimestampSchema):
    """Admin view of one variant — raw price columns (NULL = inherit)."""

    product_id: int
    title: str
    title_pl: str | None = None
    sku: str | None = None
    price_usd: float | None = None
    price_pln: float | None = None
    price_eur: float | None = None
    stock: int
    image_id: int | None = None
    position: int = 0
    is_active: bool = True
    option_value_ids: list[int] = Field(default_factory=list)


class ProductVariantPublic(BaseSchema):
    """Public (PDP) view of one **active** variant.

    Prices are the effective per-currency values (variant override, else the
    product's price in that currency) — read from the model's
    ``effective_price_*`` properties via validation aliases.
    """

    id: int
    title: str
    title_pl: str | None = None
    sku: str | None = None
    price_usd: float | None = Field(default=None, validation_alias="effective_price_usd")
    price_pln: float | None = Field(default=None, validation_alias="effective_price_pln")
    price_eur: float | None = Field(default=None, validation_alias="effective_price_eur")
    image_id: int | None = None
    image_url: str | None = None
    stock: int
    in_stock: bool
    option_value_ids: list[int] = Field(default_factory=list)


class ProductVariantUpdate(BaseSchema):
    """Single-variant PATCH body.

    ``stock`` is intentionally NOT updatable — it is the maintained sum of the
    variant's per-warehouse levels, mutated only through the inventory
    endpoints (adjust / restock / transfer). Combination changes go through
    the matrix PUT. An explicit ``null`` price clears the override (the
    variant reverts to inheriting the product's price in that currency).
    """

    sku: Annotated[str | None, Field(max_length=100)] = None
    price_usd: Annotated[float | None, Field(gt=0)] = None
    price_pln: Annotated[float | None, Field(gt=0)] = None
    price_eur: Annotated[float | None, Field(gt=0)] = None
    image_id: int | None = None
    is_active: bool | None = None
    position: Annotated[int | None, Field(ge=0)] = None


# ---------------------------------------------------------------------------
# Matrix write (PUT /products/{id}/options)
# ---------------------------------------------------------------------------


class ProductOptionValueInput(BaseSchema):
    """One desired value of an option in the matrix body."""

    value: Annotated[str, Field(min_length=1, max_length=80)]
    value_pl: Annotated[str | None, Field(min_length=1, max_length=80)] = None
    position: Annotated[int, Field(ge=0)] = 0


class ProductOptionInput(BaseSchema):
    """One desired option axis in the matrix body (max three per product)."""

    name: Annotated[str, Field(min_length=1, max_length=50)]
    name_pl: Annotated[str | None, Field(min_length=1, max_length=50)] = None
    position: Annotated[int, Field(ge=0, le=2)] = 0
    values: Annotated[list[ProductOptionValueInput], Field(min_length=1)]

    @model_validator(mode="after")
    def _values_unique(self) -> "ProductOptionInput":
        seen: set[str] = set()
        for entry in self.values:
            if entry.value in seen:
                raise ValueError(f"Duplicate value {entry.value!r} in option {self.name!r}")
            seen.add(entry.value)
        return self


class ProductVariantInput(BaseSchema):
    """One desired variant row in the matrix body.

    ``option_values`` lists the chosen value (EN string) per option, in the
    body's option order. ``initial_stock`` seeds stock for **new** variants
    only (existing variants' stock is owned by the inventory endpoints,
    mirroring the ProductUpdate "stock not updatable" rule).
    """

    id: int | None = None
    option_values: Annotated[list[str], Field(min_length=1)]
    sku: Annotated[str | None, Field(max_length=100)] = None
    price_usd: Annotated[float | None, Field(gt=0)] = None
    price_pln: Annotated[float | None, Field(gt=0)] = None
    price_eur: Annotated[float | None, Field(gt=0)] = None
    image_id: int | None = None
    is_active: bool = True
    position: Annotated[int, Field(ge=0)] = 0
    initial_stock: Annotated[int | None, Field(ge=0)] = None


class ProductOptionsMatrixUpdate(BaseSchema):
    """The matrix PUT body — the product's full desired option/variant state.

    Empty ``options`` + ``variants`` reverts the product to single-SKU (stock
    restarts at 0; restock via the inventory endpoints).
    """

    options: Annotated[list[ProductOptionInput], Field(max_length=3)]
    variants: Annotated[list[ProductVariantInput], Field(max_length=100)]

    @model_validator(mode="after")
    def _structurally_consistent(self) -> "ProductOptionsMatrixUpdate":
        names = [option.name for option in self.options]
        if len(names) != len(set(names)):
            raise ValueError("Option names must be unique")
        positions = [option.position for option in self.options]
        if len(positions) != len(set(positions)):
            raise ValueError("Option positions must be unique")
        if not self.options and self.variants:
            raise ValueError("Variants require at least one option")
        for variant in self.variants:
            if len(variant.option_values) != len(self.options):
                raise ValueError(
                    "Each variant must pick exactly one value per option "
                    f"(expected {len(self.options)}, got {len(variant.option_values)})"
                )
        return self


class ProductOptionsMatrixResponse(BaseSchema):
    """The reconciled state returned by the matrix PUT."""

    options: list[ProductOptionResponse] = Field(default_factory=list)
    variants: list[ProductVariantResponse] = Field(default_factory=list)
