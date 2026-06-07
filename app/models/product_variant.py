"""Product options + variants — the Shopify-style per-product option matrix.

A product defines up to 3 ``ProductOption`` rows (e.g. "Color", "Size"), each
with ordered ``ProductOptionValue`` rows ("Red", "XL", …). A ``ProductVariant``
is one combination of exactly one value per option, carrying its own optional
SKU / per-currency price overrides / image / stock. Variants are strictly
additive: a product with zero variant rows behaves exactly as before (product-
grain pricing + stock).

``option_signature`` denormalizes each variant's combination (sorted value ids
joined with "-") so a plain unique constraint gives the DB-level "no duplicate
combination" guard a set-valued join table can't express. The normalized source
is the ``product_variant_option_values`` join table.

``ProductVariant.stock`` is the denormalized sum across its ``stock_levels``
rows, maintained only by ``crud_inventory.apply_delta`` (the same chokepoint
discipline as ``Product.stock``).
"""

from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BaseModel

if TYPE_CHECKING:
    from app.models.product import Product


# Plain association table (composite PK, both FKs CASCADE) — the normalized
# "which value in each option" source that ``option_signature`` is derived from.
product_variant_option_values = Table(
    "product_variant_option_values",
    Base.metadata,
    Column(
        "variant_id",
        Integer,
        ForeignKey("product_variants.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "option_value_id",
        Integer,
        ForeignKey("product_option_values.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class ProductOption(BaseModel):
    """One option axis of a product (e.g. "Color"), max three per product.

    ``position`` orders the axes in the UI and in variant titles. The CHECK
    constraint is the DB-level "≤3 options" guard (positions 0-2); the CRUD
    layer also rejects a 4th option with a clean 422. The position unique
    constraint is DEFERRABLE so a reorder (position swap) within one
    transaction doesn't trip it mid-flush.
    """

    __tablename__ = "product_options"
    __table_args__ = (
        UniqueConstraint("product_id", "name", name="uq_product_options_product_name"),
        UniqueConstraint(
            "product_id",
            "position",
            name="uq_product_options_product_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("position >= 0 AND position <= 2", name="ck_product_options_max_three"),
    )

    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(50))
    # Bilingual convention mirrors Product.name_pl — nullable, EN fallback.
    name_pl: Mapped[str | None] = mapped_column(String(50))
    position: Mapped[int] = mapped_column(Integer, default=0)

    product: Mapped["Product"] = relationship("Product", back_populates="options")
    values: Mapped[list["ProductOptionValue"]] = relationship(
        "ProductOptionValue",
        back_populates="option",
        cascade="all, delete-orphan",
        order_by="ProductOptionValue.position",
    )

    def __repr__(self) -> str:
        return f"<ProductOption product#{self.product_id} {self.name!r}>"


class ProductOptionValue(BaseModel):
    """One selectable value of an option (e.g. "Red" for "Color")."""

    __tablename__ = "product_option_values"
    __table_args__ = (
        UniqueConstraint("option_id", "value", name="uq_product_option_values_option_value"),
    )

    option_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("product_options.id", ondelete="CASCADE"), index=True
    )
    value: Mapped[str] = mapped_column(String(80))
    value_pl: Mapped[str | None] = mapped_column(String(80))
    position: Mapped[int] = mapped_column(Integer, default=0)

    option: Mapped["ProductOption"] = relationship("ProductOption", back_populates="values")

    def __repr__(self) -> str:
        return f"<ProductOptionValue option#{self.option_id} {self.value!r}>"


class ProductVariant(BaseModel):
    """One purchasable combination of option values for a product.

    Prices are individually nullable — **NULL means "inherit the product's
    price in that currency"** (no "at least one" CHECK here; a full-NULL row
    is the cheap "same price as the product" case). ``stock`` is the
    denormalized sum across this variant's ``stock_levels`` rows, maintained
    only by ``crud_inventory.apply_delta``. Inactive variants are hidden from
    the public PDP and rejected at order time.
    """

    __tablename__ = "product_variants"
    __table_args__ = (
        UniqueConstraint(
            "product_id", "option_signature", name="uq_product_variants_product_signature"
        ),
    )

    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    # Denormalized "Red / XL" (EN) — rebuilt by the matrix reconcile on any
    # value rename. ``title_pl`` is the PL variant of the same.
    title: Mapped[str] = mapped_column(String(200))
    title_pl: Mapped[str | None] = mapped_column(String(200))
    # Same uniqueness scope as products.sku (global across variants).
    sku: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)

    # NULL = inherit the product's price in that currency.
    price_usd: Mapped[float | None] = mapped_column(Float)
    price_pln: Mapped[float | None] = mapped_column(Float)
    price_eur: Mapped[float | None] = mapped_column(Float)

    stock: Mapped[int] = mapped_column(Integer, default=0)
    # Optional per-variant image from the existing R2 gallery.
    image_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("product_images.id", ondelete="SET NULL")
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Sorted option_value ids joined with "-" (e.g. "12-45-87") — the
    # DB-unique denormalization of this variant's combination.
    option_signature: Mapped[str] = mapped_column(String(64))

    product: Mapped["Product"] = relationship("Product", back_populates="variants")
    option_values: Mapped[list["ProductOptionValue"]] = relationship(
        "ProductOptionValue", secondary=product_variant_option_values
    )

    def __repr__(self) -> str:
        return f"<ProductVariant product#{self.product_id} {self.title!r}>"

    @property
    def in_stock(self) -> bool:
        """True when this variant's stock > 0."""
        return self.stock > 0

    @property
    def option_value_ids(self) -> list[int]:
        """Ids of this variant's option values (for the storefront selector)."""
        return [value.id for value in self.option_values]

    @property
    def effective_price_usd(self) -> float | None:
        """USD price with NULL = inherit from the parent product."""
        return self.price_usd if self.price_usd is not None else self.product.price_usd

    @property
    def effective_price_pln(self) -> float | None:
        """PLN price with NULL = inherit from the parent product."""
        return self.price_pln if self.price_pln is not None else self.product.price_pln

    @property
    def effective_price_eur(self) -> float | None:
        """EUR price with NULL = inherit from the parent product."""
        return self.price_eur if self.price_eur is not None else self.product.price_eur

    @property
    def image_url(self) -> str | None:
        """URL of the variant's gallery image, or None when unset/missing."""
        if self.image_id is None:
            return None
        for image in self.product.images:
            if image.id == self.image_id:
                return image.url
        return None

    def get_price(self, currency: str = "USD") -> float:
        """Price in ``currency``; a NULL variant price inherits the product's.

        Mirrors ``Product.get_price`` but the fallback for an unset variant
        column is the parent product's resolution chain (which carries the
        cross-currency fallback + the at-least-one-price guarantee).
        """
        currency_map = {
            "USD": self.price_usd,
            "PLN": self.price_pln,
            "EUR": self.price_eur,
        }
        asked = currency_map.get(currency.upper())
        if asked is not None:
            return asked
        return self.product.get_price(currency)
