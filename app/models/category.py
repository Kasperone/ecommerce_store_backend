from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.product import Product


class Category(BaseModel):
    """Product category model with per-locale content + SEO meta + hero image.

    The PL columns and SEO/hero columns are all nullable so admins can fill them
    in incrementally without blocking save. The DB layer enforces uniqueness of
    ``slug_pl`` only when it is present (partial unique index) and bounds the
    meta lengths via CHECK constraints — the schema layer mirrors the same
    limits for early validation feedback.
    """

    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint("sort_order >= 0", name="ck_categories_sort_order_nonneg"),
        CheckConstraint(
            "meta_title IS NULL OR char_length(meta_title) <= 70",
            name="ck_categories_meta_title_len",
        ),
        CheckConstraint(
            "meta_description IS NULL OR char_length(meta_description) <= 160",
            name="ck_categories_meta_description_len",
        ),
        CheckConstraint(
            "meta_title_pl IS NULL OR char_length(meta_title_pl) <= 70",
            name="ck_categories_meta_title_pl_len",
        ),
        CheckConstraint(
            "meta_description_pl IS NULL OR char_length(meta_description_pl) <= 160",
            name="ck_categories_meta_description_pl_len",
        ),
        Index("ix_categories_parent_id", "parent_id"),
        Index("ix_categories_parent_sort", "parent_id", "sort_order"),
        Index(
            "uq_categories_slug_pl",
            "slug_pl",
            unique=True,
            postgresql_where=text("slug_pl IS NOT NULL"),
        ),
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

    # Polish locale content
    name_pl: Mapped[str | None] = mapped_column(String(100))
    slug_pl: Mapped[str | None] = mapped_column(String(100))
    description_pl: Mapped[str | None] = mapped_column(Text)

    # SEO meta (per locale)
    meta_title: Mapped[str | None] = mapped_column(String(70))
    meta_description: Mapped[str | None] = mapped_column(String(160))
    meta_title_pl: Mapped[str | None] = mapped_column(String(70))
    meta_description_pl: Mapped[str | None] = mapped_column(String(160))

    # Hero image (single image, 1:1 with the row)
    hero_image_url: Mapped[str | None] = mapped_column(String(500))
    hero_image_alt: Mapped[str | None] = mapped_column(String(200))
    hero_width: Mapped[int | None] = mapped_column(Integer)
    hero_height: Mapped[int | None] = mapped_column(Integer)
    hero_bytes: Mapped[int | None] = mapped_column(Integer)
    hero_mime: Mapped[str | None] = mapped_column(String(50))

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
