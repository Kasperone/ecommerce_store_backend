"""CompanyUser model — B2B company membership (the company portal foundation).

Wave D / B2B-2 slice 3. A ``company_users`` row links a ``User`` to a
``Company`` with a B2B **role** (the per-company permission axis — distinct from
``UserRole``, which is the staff/customer axis) and a membership **status**.
This is what unlocks the B2B account surfaces: a user with an ``active``
membership is a B2B account; their ``role`` gates what they can do
(VIEWER=read-only, BUYER=request quotes + checkout, COMPANY_ADMIN=profile + team,
APPROVER reserved for a deferred approval flow).

``companies.primary_user_id`` is kept as a **denormalized COMPANY_ADMIN
pointer** — the company's designated primary contact. The membership table's
migration backfills one ``active`` ``COMPANY_ADMIN`` row per company that has a
non-null ``primary_user_id`` so a primary contact resolves identically whether
the read goes through the membership table or the denormalized pointer (see
``get_current_company_member``).

``role`` / ``status`` follow the ``QuoteStatus`` / ``CompanyStatus`` convention:
a ``str``-enum stored as the (upper/lower) value in a plain ``String`` column
(not a PG enum), guarded by a CHECK constraint. Adding a value later is a
code-only change with no migration. ``COMPANY_ROLE_VALUES`` /
``COMPANY_MEMBER_STATUS_VALUES`` are the single source the DB CHECK and the
schema both reference so the layers can't drift apart.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.company import Company
    from app.models.user import User


class CompanyRole(str, enum.Enum):
    """A member's role within a company (the per-company permission axis).

    Stored as the UPPERCASE value in a ``String(16)`` column (the B2B role is a
    coarse permission level, kept upper to read distinctly from the lowercase
    lifecycle statuses). ``COMPANY_ADMIN`` manages the company profile + team;
    ``BUYER`` can request quotes and check out; ``VIEWER`` is read-only;
    ``APPROVER`` is reserved for a deferred order-approval flow. Distinct from
    ``UserRole`` (CUSTOMER/ADMIN) — a user can be a CUSTOMER globally and a
    COMPANY_ADMIN within their company.
    """

    COMPANY_ADMIN = "COMPANY_ADMIN"
    BUYER = "BUYER"
    APPROVER = "APPROVER"
    VIEWER = "VIEWER"


class CompanyMemberStatus(str, enum.Enum):
    """A membership's lifecycle status.

    Stored as the lowercase value in a ``String(16)`` column (mirrors
    ``CompanyStatus`` / ``QuoteStatus``). ``active`` is a usable membership (the
    only state that grants access); ``invited`` is a pending invitation not yet
    accepted; ``suspended`` is a deactivated membership. Only ``active``
    memberships resolve in ``get_active_membership`` / ``get_current_company_member``.
    """

    ACTIVE = "active"
    INVITED = "invited"
    SUSPENDED = "suspended"


# Mirrors of the enum value sets, used by the DB CHECK constraints and referenced
# by ``app/schemas/company.py`` so the route layer, DB layer, and OpenAPI schema
# can never drift apart. Mirrors ``QUOTE_STATUS_VALUES`` / ``COMPANY_STATUS_VALUES``.
COMPANY_ROLE_VALUES: tuple[str, ...] = tuple(r.value for r in CompanyRole)
COMPANY_MEMBER_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in CompanyMemberStatus)


class CompanyUser(BaseModel):
    """A user's membership in a company.

    ``company_id`` and ``user_id`` both ON DELETE CASCADE — a membership is
    meaningless without its company or its user, so deleting either takes the
    membership row with it. A user can belong to a company **at most once**
    (unique ``(company_id, user_id)``). ``role`` / ``status`` are CHECK-guarded
    to their enum value sets. ``invited_email`` / ``invited_at`` capture an
    invitation that has not yet been bound to a concrete user account (team
    management, Wave D slice 5) — both nullable; a directly-created (e.g.
    backfilled or business-signup) membership leaves them ``None``.
    """

    __tablename__ = "company_users"
    __table_args__ = (
        UniqueConstraint("company_id", "user_id", name="uq_company_users_company_user"),
        CheckConstraint(
            "role IN ('" + "', '".join(COMPANY_ROLE_VALUES) + "')",
            name="ck_company_users_role_enum",
        ),
        CheckConstraint(
            "status IN ('" + "', '".join(COMPANY_MEMBER_STATUS_VALUES) + "')",
            name="ck_company_users_status_enum",
        ),
    )

    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    invited_email: Mapped[str | None] = mapped_column(String(255))
    invited_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Relationships. ``company.members`` is the inverse collection (back_populates).
    # ``user`` is plain (the User side has no membership collection in the MVP).
    company: Mapped[Company] = relationship("Company", back_populates="members")
    user: Mapped[User] = relationship("User")

    def __repr__(self) -> str:
        return (
            f"<CompanyUser company#{self.company_id} user#{self.user_id} "
            f"{self.role} {self.status!r}>"
        )
