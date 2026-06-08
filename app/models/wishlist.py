"""WishlistItem model — a logged-in customer's saved products.

A wishlist item records that a user wants to remember a product, optionally a
specific ``ProductVariant`` of it. It is strictly owner-scoped: every lookup and
mutation goes through ``(id, user_id)`` so a caller can only ever touch their own
rows (mirrors the address book).

All three FKs ON DELETE CASCADE — a deleted account, a deleted product, or a
deleted variant takes the wishlist row with it (a saved item that points at a
gone product has no meaning). ``variant_id`` is nullable: a variantless save is
the common case (the whole product), and a variant save pins one combination.

The UNIQUE on ``(user_id, product_id, variant_id)`` stops the same
product+variant being saved twice. Postgres treats NULLs as distinct in a UNIQUE
index, so a user *can* hold both a variantless row and one-or-more variant rows
for the same product — that is the intended shape (the whole product plus a
couple of pinned variants), and a re-add of an identical variantless row is
caught by the CRUD/route idempotency check rather than the constraint.
"""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.product_variant import ProductVariant
    from app.models.user import User


class WishlistItem(BaseModel):
    """One saved product (optionally a specific variant) in a user's wishlist."""

    __tablename__ = "wishlist_items"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "product_id",
            "variant_id",
            name="uq_wishlist_items_user_product_variant",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("product_variants.id", ondelete="CASCADE"), index=True
    )

    user: Mapped["User"] = relationship("User", back_populates="wishlist_items")
    product: Mapped["Product"] = relationship("Product")
    variant: Mapped["ProductVariant | None"] = relationship("ProductVariant")

    def __repr__(self) -> str:
        return (
            f"<WishlistItem {self.id} user={self.user_id} "
            f"product={self.product_id} variant={self.variant_id}>"
        )
