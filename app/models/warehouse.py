from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.product_variant import ProductVariant


class Warehouse(BaseModel):
    """A physical (or logical) stock location.

    Multi-warehouse v1: every product's stock is tracked per-warehouse via
    ``StockLevel`` rows, and ``Product.stock`` is the maintained sum across them
    (kept correct by routing every change through ``crud_inventory.apply_delta``).

    Exactly one warehouse is the **default** — orders are fulfilled from it (v1
    fulfilment strategy) and the migration backfills all existing stock into it.
    The default is enforced by a partial unique index so two rows can never both
    claim it.
    """

    __tablename__ = "warehouses"
    __table_args__ = (
        # At most one row with is_default=true (partial unique index — only the
        # true rows participate, so the single value collides on a second default).
        Index(
            "uq_warehouses_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    name: Mapped[str] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    stock_levels: Mapped[list["StockLevel"]] = relationship(
        "StockLevel", back_populates="warehouse"
    )

    def __repr__(self) -> str:
        return f"<Warehouse {self.code}{' *' if self.is_default else ''}>"


class StockLevel(BaseModel):
    """Per-warehouse stock quantity for a product (the per-location source of truth).

    ``Product.stock`` is the denormalized total across these rows. Created lazily
    on the first movement into a (product, warehouse) pair via
    ``crud_inventory.apply_delta``. ``variant_id`` is NULL for product-grain
    rows (variantless products) and set for variant-grain rows; the unique
    constraint uses PG16's NULLS NOT DISTINCT so two NULL-variant rows for the
    same (product, warehouse) collide like any other duplicate. The warehouse
    FK is RESTRICT so a warehouse holding stock can't be hard-deleted (admins
    deactivate instead).
    """

    __tablename__ = "stock_levels"
    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "warehouse_id",
            "variant_id",
            name="uq_stock_levels_product_warehouse_variant",
            postgresql_nulls_not_distinct=True,
        ),
    )

    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    warehouse_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("warehouses.id", ondelete="RESTRICT"), index=True
    )
    variant_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("product_variants.id", ondelete="CASCADE"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, default=0)

    product: Mapped["Product"] = relationship("Product", back_populates="stock_levels")
    warehouse: Mapped["Warehouse"] = relationship("Warehouse", back_populates="stock_levels")
    variant: Mapped["ProductVariant | None"] = relationship("ProductVariant")

    def __repr__(self) -> str:
        variant = f" variant#{self.variant_id}" if self.variant_id is not None else ""
        return (
            f"<StockLevel product#{self.product_id} wh#{self.warehouse_id}"
            f"{variant} = {self.quantity}>"
        )
