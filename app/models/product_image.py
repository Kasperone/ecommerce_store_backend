from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.product import Product


class ProductImage(BaseModel):
    """One image attached to a product.

    Replaces the legacy ``products.images`` JSON column with a row-per-image
    model that carries dimensions, byte size, magic-byte-validated MIME, and
    primary/sort metadata. Image creation, mutation, and deletion happen via
    the dedicated admin upload endpoints (see PR1 of the catalog plan).
    """

    __tablename__ = "product_images"

    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(String(500))
    alt_text: Mapped[str | None] = mapped_column(String(200))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    # Captured at upload time by the image service (Pillow + filetype sniff).
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    bytes: Mapped[int] = mapped_column(Integer)
    mime: Mapped[str] = mapped_column(String(50))

    product: Mapped["Product"] = relationship("Product", back_populates="images")

    __table_args__ = (
        # DB-level guarantee that each product has at most one primary image.
        # Partial unique index — only rows where is_primary=true participate.
        Index(
            "uq_product_images_primary_per_product",
            "product_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )

    def __repr__(self) -> str:
        return f"<ProductImage product={self.product_id} #{self.id} primary={self.is_primary}>"
