from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.category import Category
    from app.models.order import OrderItem
    from app.models.product_image import ProductImage


class Product(BaseModel):
    """Product model with multi-currency support"""

    __tablename__ = "products"

    # Basic info
    name: Mapped[str] = mapped_column(String(200), index=True)
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    sku: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)

    # Pricing (multi-currency)
    price_usd: Mapped[float] = mapped_column(Float)
    price_pln: Mapped[float] = mapped_column(Float)
    price_eur: Mapped[float] = mapped_column(Float)

    # Inventory
    stock: Mapped[int] = mapped_column(Integer, default=0)
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

    def __repr__(self):
        return f"<Product {self.name}>"

    @property
    def in_stock(self):
        """Check if product is in stock"""
        return self.stock > 0

    def get_price(self, currency: str = "USD") -> float:
        """Get price for specific currency"""
        currency_map = {
            "USD": self.price_usd,
            "PLN": self.price_pln,
            "EUR": self.price_eur,
        }
        return currency_map.get(currency.upper(), self.price_usd)
