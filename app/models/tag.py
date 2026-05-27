"""Customer tags — a normalized, reusable vocabulary.

A ``Tag`` is a shared label (``"VIP"``, ``"wholesale"``, …) that admins attach to
customers via the ``customer_tags`` join. Normalized (not a per-row array) so a
tag can be renamed/recolored once and reflect everywhere, autocompleted from the
existing set, and used to filter the customer list with an index-backed join.

Tag names are unique case-insensitively (``uq_tags_name_lower`` functional index;
hand-added in the migration — autogenerate doesn't emit functional indexes).
Per-assignment who/when is not stored on the join; the audit log records it
(``resource_type="customer_tag"`` for assign/detach, ``"tag"`` for the vocabulary).
"""

from typing import TYPE_CHECKING

from sqlalchemy import Column, ForeignKey, Index, Integer, String, Table, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BaseModel

if TYPE_CHECKING:
    from app.models.user import User


# Plain association table (composite PK, both FKs CASCADE). Referenced by name
# as the ``secondary`` of the User/Tag relationships.
customer_tags = Table(
    "customer_tags",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Tag(BaseModel):
    """A reusable customer label."""

    __tablename__ = "tags"
    __table_args__ = (Index("uq_tags_name_lower", text("lower(name)"), unique=True),)

    name: Mapped[str] = mapped_column(String(50))
    # Optional hex chip color (e.g. "#3b82f6"); the frontend defaults when null.
    color: Mapped[str | None] = mapped_column(String(7))

    customers: Mapped[list["User"]] = relationship(
        "User", secondary=customer_tags, back_populates="tags"
    )

    def __repr__(self) -> str:
        return f"<Tag {self.name!r}>"
