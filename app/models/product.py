from sqlalchemy import Column, String, Text, Integer, Float, Boolean, ForeignKey, JSON
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class Product(BaseModel):
    """Product model with multi-currency support"""
    
    __tablename__ = "products"
    
    # Basic info
    name = Column(String(200), nullable=False, index=True)
    slug = Column(String(200), unique=True, index=True, nullable=False)
    description = Column(Text, nullable=True)
    sku = Column(String(100), unique=True, index=True, nullable=True)
    
    # Pricing (multi-currency)
    price_usd = Column(Float, nullable=False)
    price_pln = Column(Float, nullable=False)
    price_eur = Column(Float, nullable=False)
    
    # Inventory
    stock = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    is_featured = Column(Boolean, default=False, nullable=False)
    
    # Images (JSON array of image URLs)
    images = Column(JSON, default=list, nullable=False)
    
    # Category
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    
    # Relationships
    category = relationship("Category", back_populates="products")
    order_items = relationship("OrderItem", back_populates="product")
    
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
