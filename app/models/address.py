"""Address model — a first-class entry in a user's address book.

Promotes the inline ``users.shipping_*`` columns into their own table so a user
can keep several addresses and flag one as the default for shipping and one for
billing (they may be the same row). The ``users.shipping_*`` columns are kept for
now (dropped in a later follow-up per D-ACCT-2) and back-filled into one default
address per user by this table's migration.

Default mutual-exclusion (``is_default_shipping`` / ``is_default_billing``) is
enforced in the CRUD layer transactionally — setting a new default clears the
flag on the user's other rows in the same unit of work — not by a DB constraint,
so the rule lives in one place alongside the auto-default-first-address behaviour.
"""

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.user import User


class Address(BaseModel):
    """A saved address belonging to a user.

    ``user_id`` is indexed and cascades on user delete. ``recipient_name`` /
    ``street`` / ``city`` / ``postal_code`` / ``country`` are required; ``label``
    (e.g. "Home", "Work"), ``state`` and ``phone`` are optional. The two
    ``is_default_*`` flags are mutually exclusive *within a kind* across a user's
    rows — guaranteed by the CRUD layer, not the DB.
    """

    __tablename__ = "addresses"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str | None] = mapped_column(String(100))
    recipient_name: Mapped[str] = mapped_column(String(255))
    street: Mapped[str] = mapped_column(String(255))
    city: Mapped[str] = mapped_column(String(100))
    postal_code: Mapped[str] = mapped_column(String(20))
    country: Mapped[str] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(20))
    is_default_shipping: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    is_default_billing: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    user: Mapped["User"] = relationship("User", back_populates="addresses")

    def __repr__(self) -> str:
        return f"<Address {self.id} user={self.user_id} {self.city}>"
