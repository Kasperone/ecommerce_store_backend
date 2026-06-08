"""Manual collections — curated product groupings outside the category tree.

A ``Collection`` mirrors :class:`~app.models.category.Category` (per-locale
name/slug/description + SEO meta + hero image) but lives in its own
``/collections/...`` slug namespace, disjoint from categories — so the two never
collide. Membership is materialized into the ``collection_products`` join with an
explicit ``position`` so the read/index path is index-backed and ordered.

Forward-compat for *smart* collections is baked in but **inert** in v1: a
``type`` column (``'manual' | 'smart'``, CHECK-constrained, default ``'manual'``)
and a nullable ``rules`` JSON column. v1 only ever writes ``manual``/``NULL`` —
materializing membership into the same join means a future smart-collection
recompute job layers on with zero rewrite of the read/index path.

DB discipline copied verbatim from ``Category``: global unique ``slug`` + a
partial unique index on ``slug_pl WHERE NOT NULL`` (hand-added in the migration —
autogenerate emits neither) + CHECK constraints on ``sort_order`` and the meta
lengths. The route layer adds a 400 collision check so a slug clash is a clean
400, not a 500.
"""

from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BaseModel

if TYPE_CHECKING:
    from app.models.product import Product


# Membership join (composite PK, both FKs CASCADE, explicit curation order).
# Plain Table — position is written by the membership CRUD; lighter than an
# association object and sufficient for the ordered-set replace pattern.
collection_products = Table(
    "collection_products",
    Base.metadata,
    Column(
        "collection_id",
        Integer,
        ForeignKey("collections.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "product_id",
        Integer,
        ForeignKey("products.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("position", Integer, nullable=False, server_default="0", default=0),
    Index("ix_collection_products_collection_position", "collection_id", "position"),
)


class Collection(BaseModel):
    """A curated, manually-managed grouping of products (Shopify-style collection).

    Columns mirror :class:`Category` (per-locale content + SEO + hero) plus the
    inert ``type``/``rules`` smart-collection forward-compat pair.
    """

    __tablename__ = "collections"
    __table_args__ = (
        CheckConstraint("sort_order >= 0", name="ck_collections_sort_order_nonneg"),
        CheckConstraint("type IN ('manual','smart')", name="ck_collections_type"),
        CheckConstraint(
            "meta_title IS NULL OR char_length(meta_title) <= 70",
            name="ck_collections_meta_title_len",
        ),
        CheckConstraint(
            "meta_description IS NULL OR char_length(meta_description) <= 160",
            name="ck_collections_meta_description_len",
        ),
        CheckConstraint(
            "meta_title_pl IS NULL OR char_length(meta_title_pl) <= 70",
            name="ck_collections_meta_title_pl_len",
        ),
        CheckConstraint(
            "meta_description_pl IS NULL OR char_length(meta_description_pl) <= 160",
            name="ck_collections_meta_description_pl_len",
        ),
        Index(
            "uq_collections_slug_pl",
            "slug_pl",
            unique=True,
            postgresql_where=text("slug_pl IS NOT NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
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

    # Smart-collection forward-compat — inert in v1 (only 'manual'/NULL written).
    type: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual")
    rules: Mapped[dict | None] = mapped_column(JSON)

    products: Mapped[list["Product"]] = relationship(
        "Product",
        secondary=collection_products,
        order_by=collection_products.c.position,
    )

    def __repr__(self) -> str:
        return f"<Collection {self.name!r}>"
