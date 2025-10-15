from sqlalchemy import Column, String, Integer, Float, ForeignKey, Enum as SQLEnum, Text
from sqlalchemy.orm import relationship
import enum
from app.models.base import BaseModel


class OrderStatus(str, enum.Enum):
    """Order status enum"""
    PENDING = "pending"
    PAID = "paid"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class Order(BaseModel):
    """Order model"""
    
    __tablename__ = "orders"
    
    # User
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    # Order details
    order_number = Column(String(50), unique=True, index=True, nullable=False)
    status = Column(SQLEnum(OrderStatus), default=OrderStatus.PENDING, nullable=False)
    
    # Pricing
    subtotal = Column(Float, nullable=False)
    tax = Column(Float, default=0, nullable=False)
    shipping = Column(Float, default=0, nullable=False)
    total = Column(Float, nullable=False)
    currency = Column(String(3), default="USD", nullable=False)  # USD, PLN, EUR
    
    # Shipping address
    shipping_address = Column(Text, nullable=False)
    shipping_city = Column(String(100), nullable=False)
    shipping_postal_code = Column(String(20), nullable=False)
    shipping_country = Column(String(100), nullable=False)
    
    # Payment
    payment_method = Column(String(50), nullable=True)  # stripe, payu, etc
    payment_intent_id = Column(String(255), nullable=True)  # From payment provider
    
    # Notes
    notes = Column(Text, nullable=True)
    
    # Relationships
    user = relationship("User", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Order {self.order_number}>"


class OrderItem(BaseModel):
    """Order item model (products in an order)"""
    
    __tablename__ = "order_items"
    
    # Order
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    
    # Product
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    
    # Item details (snapshot at time of order)
    product_name = Column(String(200), nullable=False)
    product_sku = Column(String(100), nullable=True)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Float, nullable=False)
    total_price = Column(Float, nullable=False)
    
    # Relationships
    order = relationship("Order", back_populates="items")
    product = relationship("Product", back_populates="order_items")
    
    def __repr__(self):
        return f"<OrderItem {self.product_name} x{self.quantity}>"
