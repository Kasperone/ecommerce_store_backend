from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.product import Product


class Category(BaseModel):
    """Product category model"""

    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint("sort_order >= 0", name="ck_categories_sort_order_nonneg"),
        Index("ix_categories_parent_id", "parent_id"),
        Index("ix_categories_parent_sort", "parent_id", "sort_order"),
    )

    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    parent_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("categories.id", ondelete="RESTRICT"),
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    parent: Mapped["Category | None"] = relationship(
        "Category",
        remote_side="Category.id",
        back_populates="children",
    )
    children: Mapped[list["Category"]] = relationship(
        "Category",
        back_populates="parent",
        order_by="(Category.sort_order, Category.id)",
    )

    products: Mapped[list["Product"]] = relationship("Product", back_populates="category")

    def __repr__(self):
        return f"<Category {self.name}>"
