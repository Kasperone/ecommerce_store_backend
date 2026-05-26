import logging
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.models.category import Category
    from app.models.inventory_movement import InventoryMovement
    from app.models.order import OrderItem
    from app.models.product_image import ProductImage
    from app.models.review import Review


class Product(BaseModel):
    """Product model with multi-currency support"""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(
            "price_usd IS NOT NULL OR price_pln IS NOT NULL OR price_eur IS NOT NULL",
            name="ck_products_at_least_one_price",
        ),
    )

    # Basic info
    name: Mapped[str] = mapped_column(String(200), index=True)
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    sku: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)

    # Pricing (multi-currency) — individually nullable; ck_products_at_least_one_price
    # guarantees at least one is set at the DB level
    price_usd: Mapped[float | None] = mapped_column(Float)
    price_pln: Mapped[float | None] = mapped_column(Float)
    price_eur: Mapped[float | None] = mapped_column(Float)

    # Inventory
    stock: Mapped[int] = mapped_column(Integer, default=0)
    # Null = no threshold set → the product is never flagged "low stock".
    low_stock_threshold: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False)

    # Category
    category_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("categories.id"))

    # Relationships
    category: Mapped["Category | None"] = relationship("Category", back_populates="products")
    order_items: Mapped[list["OrderItem"]] = relationship("OrderItem", back_populates="product")
    images: Mapped[list["ProductImage"]] = relationship(
        "ProductImage",
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductImage.sort_order",
    )
    movements: Mapped[list["InventoryMovement"]] = relationship(
        "InventoryMovement",
        back_populates="product",
        cascade="all, delete-orphan",
    )
    reviews: Mapped[list["Review"]] = relationship(
        "Review",
        back_populates="product",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Product {self.name}>"

    @property
    def in_stock(self):
        """Check if product is in stock"""
        return self.stock > 0

    def get_price(self, currency: str = "USD") -> float:
        """Return price in ``currency`` with deterministic fallback (PLN → USD → EUR).

        The DB CHECK constraint guarantees at least one currency is set, so the
        ``0.0`` return is only reachable for rows inserted before that constraint
        shipped — logged as a warning rather than raising to keep storefront reads
        resilient.
        """
        currency_map = {
            "USD": self.price_usd,
            "PLN": self.price_pln,
            "EUR": self.price_eur,
        }
        asked = currency_map.get(currency.upper())
        if asked is not None:
            return asked
        for fallback in (self.price_pln, self.price_usd, self.price_eur):
            if fallback is not None:
                return fallback
        logger.warning("Product %s has no prices set in any currency", self.id)
        return 0.0
