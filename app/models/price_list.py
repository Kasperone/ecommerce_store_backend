"""Price-list models — B2B negotiated per-product pricing (Shopify-gap P2 #14).

A ``PriceList`` is admin-managed management metadata (name + active flag +
notes; same posture as coupons / companies): a named set of explicit negotiated
prices. Its ``PriceListEntry`` rows pin a per-currency price for a product (or a
specific variant). A company is assigned at most one price list
(``companies.price_list_id``); when an **active company member** shops, each
order line for a product that has an entry is priced from the list instead of
the catalog (products with no entry fall back to the catalog price).

**Explicit per-product prices only** (the user's Program-1 decision) — no
percentage-off, no min-qty tiers, no validity window. Those are deferred.

Prices are ``Numeric(12,2)`` / ``Decimal`` like every money column, individually
nullable, guarded by an at-least-one-price CHECK (mirrors
``ck_products_at_least_one_price``). An entry that lacks the shopper's currency
falls back to the catalog price for that currency (the resolver returns ``None``
→ caller keeps catalog), so a PLN-only list never zero-prices a USD order.

``(price_list_id, product_id, variant_id)`` is UNIQUE — one entry per grain per
list. A NULL ``variant_id`` is the **product-grain** entry (applies to a
non-variant product, or as a fallback for a variant product); a non-NULL
``variant_id`` is a **variant-grain** entry that wins over the product-grain
entry for that specific variant.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.product_variant import ProductVariant


class PriceList(BaseModel):
    """An admin-managed B2B price list assigned to companies.

    ``name`` is required; ``notes`` is free-form; ``is_active`` gates whether
    the list is applied at checkout (an inactive list is ignored — the buyer
    falls back to catalog everywhere). ``price_lists.id`` is the stable contract
    ``companies.price_list_id`` FKs against.
    """

    __tablename__ = "price_lists"

    name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    notes: Mapped[str | None] = mapped_column(Text)

    entries: Mapped[list[PriceListEntry]] = relationship(
        "PriceListEntry",
        back_populates="price_list",
        cascade="all, delete-orphan",
        order_by="PriceListEntry.id",
    )

    @property
    def entry_count(self) -> int:
        """Number of entries (eager-loaded in scoped queries — no N+1)."""
        return len(self.entries)

    def __repr__(self) -> str:
        return f"<PriceList {self.id} {self.name!r} active={self.is_active}>"


class PriceListEntry(BaseModel):
    """A single negotiated price for a product (or variant) in a price list.

    ``price_list_id`` is ON DELETE CASCADE (an entry is meaningless without its
    list; matches the ``delete-orphan`` relationship cascade). ``product_id`` is
    ON DELETE CASCADE — a negotiated price without its product is dead data.
    ``variant_id`` is nullable + ON DELETE CASCADE: NULL = product-grain entry;
    a non-NULL variant-grain entry is dropped if that variant is deleted (the
    product-grain entry, if any, survives). ``price_usd``/``price_pln``/
    ``price_eur`` are individually nullable, at least one enforced by CHECK.
    """

    __tablename__ = "price_list_entries"
    __table_args__ = (
        UniqueConstraint(
            "price_list_id",
            "product_id",
            "variant_id",
            name="uq_price_list_entries_grain",
        ),
        CheckConstraint(
            "price_usd IS NOT NULL OR price_pln IS NOT NULL OR price_eur IS NOT NULL",
            name="ck_price_list_entries_at_least_one_price",
        ),
        CheckConstraint(
            "price_usd IS NULL OR price_usd >= 0",
            name="ck_price_list_entries_price_usd_nonneg",
        ),
        CheckConstraint(
            "price_pln IS NULL OR price_pln >= 0",
            name="ck_price_list_entries_price_pln_nonneg",
        ),
        CheckConstraint(
            "price_eur IS NULL OR price_eur >= 0",
            name="ck_price_list_entries_price_eur_nonneg",
        ),
    )

    price_list_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("price_lists.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("product_variants.id", ondelete="CASCADE"), index=True
    )
    price_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_pln: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    price_eur: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    price_list: Mapped[PriceList] = relationship("PriceList", back_populates="entries")
    product: Mapped[Product] = relationship("Product")
    variant: Mapped[ProductVariant | None] = relationship("ProductVariant")

    def get_price(self, currency: str = "USD") -> Decimal | None:
        """Return this entry's price in ``currency``, or ``None`` if unset.

        ``None`` means "no negotiated price in this currency" → the caller keeps
        the catalog price for that currency (no cross-currency fallback: a
        PLN-only entry must not price a USD order). Distinct from
        ``Product.get_price`` which falls back across currencies.
        """
        return {
            "USD": self.price_usd,
            "PLN": self.price_pln,
            "EUR": self.price_eur,
        }.get(currency.upper())

    @property
    def product_name(self) -> str | None:
        """Denormalized product name for the admin entry editor (from_attributes)."""
        return self.product.name if self.product is not None else None

    @property
    def variant_title(self) -> str | None:
        """Denormalized variant title (None for product-grain entries)."""
        return self.variant.title if self.variant is not None else None

    def __repr__(self) -> str:
        grain = f"product#{self.product_id}"
        if self.variant_id is not None:
            grain += f" variant#{self.variant_id}"
        return f"<PriceListEntry list#{self.price_list_id} {grain}>"
