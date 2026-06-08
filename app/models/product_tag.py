"""Product tags — a normalized, reusable merchandising vocabulary.

A ``ProductTag`` is a shared label (``"sale"``, ``"new-arrival"``, …) that admins
attach to products via the ``product_tag_links`` join. Normalized (not a per-row
array) so a tag can be renamed/recolored once and reflect everywhere,
autocompleted from the existing set, faceted in Meili, and used to filter the
public product list with an index-backed join.

This deliberately MIRRORS the customer-tag system (``app/models/tag.py``) but uses
strictly distinct names — model ``ProductTag``, tables ``product_tags`` +
``product_tag_links``, audit ``resource_type="product_tag"`` — so the two tag
systems never collide. Reusing any customer-tag name here is a hard error.

Tag names are unique case-insensitively (``uq_product_tags_name_lower`` functional
index; hand-added in the migration — autogenerate doesn't emit functional
indexes). The audit log records the vocabulary CRUD (``resource_type="product_tag"``)
and the link changes (``resource_type="product_tag_link"``).
"""

from typing import TYPE_CHECKING

from sqlalchemy import Column, ForeignKey, Index, Integer, String, Table, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BaseModel

if TYPE_CHECKING:
    from app.models.product import Product


# Plain association table (composite PK, both FKs CASCADE). Referenced by name
# as the ``secondary`` of the Product/ProductTag relationships. Named
# ``product_tag_links`` (NOT ``product_tags`` — that's the vocabulary table, and
# never ``customer_tags``) to keep the two tag systems disjoint.
product_tag_links = Table(
    "product_tag_links",
    Base.metadata,
    Column("product_id", Integer, ForeignKey("products.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("product_tags.id", ondelete="CASCADE"), primary_key=True),
)


class ProductTag(BaseModel):
    """A reusable product merchandising label."""

    __tablename__ = "product_tags"
    __table_args__ = (Index("uq_product_tags_name_lower", text("lower(name)"), unique=True),)

    name: Mapped[str] = mapped_column(String(50))
    # Optional hex chip color (e.g. "#3b82f6"); the frontend defaults when null.
    color: Mapped[str | None] = mapped_column(String(7))

    products: Mapped[list["Product"]] = relationship(
        "Product", secondary=product_tag_links, back_populates="tags"
    )

    def __repr__(self) -> str:
        return f"<ProductTag {self.name!r}>"
