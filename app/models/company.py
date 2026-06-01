"""Company model — admin-managed B2B company directory.

This is **management metadata only** (same posture as coupons / campaigns): a
company records a name, optional tax id / contact details, a lifecycle status,
an optional account-manager label, and free-form notes. **An approval workflow
beyond the status field, and linking customer users to companies, are
deliberately NOT modelled here** — those are separate, deferred concerns. The
admin CRUD only manages the rows.

``status`` follows the ``RefundReason`` / ``CampaignStatus`` convention: a
``str``-enum stored as the lowercase value in a plain ``String(16)`` column (not
a PG enum), guarded by a CHECK constraint. Adding a value later is a code-only
change with no migration. ``account_manager`` is a free-text label for now — a
``User`` FK is deferred.
"""

from __future__ import annotations

import enum

from sqlalchemy import (
    CheckConstraint,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class CompanyStatus(str, enum.Enum):
    """A company's directory status.

    Stored as the lowercase value in a ``String(16)`` column (mirrors
    ``RefundReason``). ``PENDING`` is the default for a freshly added company;
    ``APPROVED`` / ``REJECTED`` are set by an admin. A richer approval workflow
    is deferred.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# Mirror of ``CompanyStatus.values``, used by the DB CHECK constraint and
# referenced by ``app/schemas/company.py`` so the route layer, DB layer, and
# OpenAPI schema can never drift apart. Mirrors ``REFUND_REASON_VALUES``.
COMPANY_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in CompanyStatus)


class Company(BaseModel):
    """An admin-managed B2B company directory entry.

    ``name`` is required; ``tax_id``, ``email``, ``phone``, ``account_manager``
    and ``notes`` are all optional. ``status`` defaults to ``pending``.
    ``companies.id`` (the integer PK) is the stable contract the quotes vertical
    FKs against (``quotes.company_id -> companies.id``).
    """

    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "', '".join(COMPANY_STATUS_VALUES) + "')",
            name="ck_companies_status_enum",
        ),
    )

    name: Mapped[str] = mapped_column(String(255))
    tax_id: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    account_manager: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<Company {self.id} {self.status!r} {self.name!r}>"
