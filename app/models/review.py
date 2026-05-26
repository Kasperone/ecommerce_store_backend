import enum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.user import User


class ReviewStatus(str, enum.Enum):
    """Moderation state of a customer review.

    Stored as the lowercase value string in a plain ``String`` column (not a PG
    enum type) so adding a state is a code-only change, no migration — same
    pattern as ``InventoryMovementReason``. Only ``APPROVED`` reviews are public.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Review(BaseModel):
    """A customer's rating + written review of a product.

    One review per ``(product, user)`` — enforced by ``uq_reviews_product_user``;
    a second submission is a 409 and the customer edits/deletes the existing row
    instead. New reviews start ``pending`` and surface publicly only once an
    admin approves them. ``is_verified_purchase`` is snapshotted at creation from
    the user's order history (a non-cancelled order containing the product), so a
    later cancellation doesn't retroactively strip the badge.

    Both foreign keys are ON DELETE CASCADE so a review never outlives its
    product or its author.
    """

    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_reviews_rating_range"),
        UniqueConstraint("product_id", "user_id", name="uq_reviews_product_user"),
    )

    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    rating: Mapped[int] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=ReviewStatus.PENDING.value, index=True)
    is_verified_purchase: Mapped[bool] = mapped_column(Boolean, default=False)

    product: Mapped["Product"] = relationship("Product", back_populates="reviews")
    user: Mapped["User"] = relationship("User", back_populates="reviews")

    def __repr__(self) -> str:
        return (
            f"<Review product#{self.product_id} user#{self.user_id} "
            f"rating={self.rating} ({self.status})>"
        )

    @property
    def author_name(self) -> str:
        """Privacy-conscious display name for public review attribution.

        First name + last initial (``"Kasper O."``). Relies on ``self.user``
        being eager-loaded by the query (the public/admin queries selectinload
        it — same pattern as ``Order.customer_email``).
        """
        if self.user is None:
            return "Anonymous"
        first = (self.user.first_name or "").strip()
        last = (self.user.last_name or "").strip()
        if first and last:
            return f"{first} {last[0]}."
        return first or "Anonymous"
