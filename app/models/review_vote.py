from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.review import Review


class ReviewVote(BaseModel):
    """A customer's "helpful" vote on an approved review.

    One row per ``(review, user)`` — enforced by ``uq_review_votes_review_user``,
    so re-voting is idempotent (``crud_review_vote.cast_vote`` no-ops when the row
    already exists) and a customer can never inflate a count. This table is the
    per-user detail; the displayed total lives denormalized on
    ``Review.helpful_count``, which ``crud_review_vote`` keeps in step in the same
    transaction (the choke-point discipline — see ``crud_inventory.apply_delta``).

    Both foreign keys are ON DELETE CASCADE so a vote never outlives its review or
    its voter.
    """

    __tablename__ = "review_votes"
    __table_args__ = (UniqueConstraint("review_id", "user_id", name="uq_review_votes_review_user"),)

    review_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reviews.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    review: Mapped["Review"] = relationship("Review", back_populates="votes")

    def __repr__(self) -> str:
        return f"<ReviewVote review#{self.review_id} user#{self.user_id}>"
