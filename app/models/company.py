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
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel
from app.models.company_user import CompanyRole

if TYPE_CHECKING:
    from app.models.company_user import CompanyUser
    from app.models.payment_term import PaymentTerm
    from app.models.price_list import PriceList
    from app.models.user import User


# Default set of membership roles allowed to place a pay-on-account order when a
# company enables net terms (net-terms tail Program A). VIEWER (read-only) is
# excluded; an admin can narrow or widen this per company.
DEFAULT_NET_TERMS_ROLES: list[str] = [
    CompanyRole.COMPANY_ADMIN.value,
    CompanyRole.APPROVER.value,
    CompanyRole.BUYER.value,
]


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
    # B2B company profile fields (Wave D / D-D-4). All nullable — a company is
    # created via the admin directory or business self-signup with just a name +
    # tax id, then a COMPANY_ADMIN fills these in via ``PATCH /companies/me``.
    # ``regon`` is the Polish business registry number (sits alongside ``tax_id``
    # which holds the NIP/VAT id). ``billing_address_*`` is the invoice address
    # (distinct from the per-user shipping address). ``default_currency`` is the
    # company's preferred 3-letter ISO currency for quotes/orders.
    regon: Mapped[str | None] = mapped_column(String(32))
    billing_address_street: Mapped[str | None] = mapped_column(String(255))
    billing_address_city: Mapped[str | None] = mapped_column(String(128))
    billing_address_postal_code: Mapped[str | None] = mapped_column(String(32))
    billing_address_country: Mapped[str | None] = mapped_column(String(128))
    billing_address_state: Mapped[str | None] = mapped_column(String(128))
    default_currency: Mapped[str | None] = mapped_column(String(3))
    # Designated primary contact customer. The user an admin picks as the company's
    # contact; a quote→order conversion attributes the resulting order to this real
    # customer (correct emails/history/invoices) rather than to the converting admin.
    # Nullable (companies start without one) and ON DELETE SET NULL so deleting the
    # user clears the link without blocking the user delete.
    primary_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    # Assigned B2B price list (Program 1 / Shopify-gap P2 #14). Nullable — most
    # companies have none and pay catalog prices. ON DELETE SET NULL so deleting
    # a price list unassigns every company that used it rather than blocking the
    # delete (mirrors the ``primary_user_id`` posture). When set to an *active*
    # list, an active member's checkout lines are priced from the list.
    price_list_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("price_lists.id", ondelete="SET NULL"), index=True
    )
    # B2B net terms (Program 2, generalised by the tail's Program A).
    # ``net_terms_enabled`` is the master toggle; ``payment_term_id`` points at a
    # named ``PaymentTerm`` template (Net-N / due-on-receipt / fixed-date) that
    # decides the printed due date — it replaces the old bare ``net_terms_days``
    # int. An active member whose role is in ``net_terms_allowed_roles`` may choose
    # "pay on account" at checkout: the order is placed unpaid with the term's due
    # date, the faktura is issued at placement, the merchant records the transfer
    # later. ``credit_limit`` (in ``default_currency``; NULL = unlimited) hard-blocks
    # the option once outstanding unpaid balance + the new order would exceed it
    # (card/PayU/COD stay available). Dark by default — no terms until an admin
    # enables them AND assigns an active payment term.
    net_terms_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    payment_term_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("payment_terms.id", ondelete="SET NULL"), index=True
    )
    # Membership roles allowed to pay on account (per-company gate). Stored as a
    # JSON list of ``CompanyRole`` values; defaults to admin+approver+buyer
    # (VIEWER excluded). A member whose role is absent never sees the option.
    net_terms_allowed_roles: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: list(DEFAULT_NET_TERMS_ROLES), nullable=False
    )
    credit_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # B2B net-terms auto-charge (tail Program C). Opt-in per company: when
    # ``auto_charge_enabled`` AND a card is vaulted, a cron off-session-charges the
    # net-terms order at term end instead of waiting for a manual bank transfer
    # (which stays the default for every other net-terms company).
    # ``stripe_customer_id`` + ``auto_charge_payment_method_id`` are the vaulted
    # Stripe handles, set via the company portal's SetupIntent flow.
    auto_charge_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255))
    auto_charge_payment_method_id: Mapped[str | None] = mapped_column(String(255))

    # Loaded for the denormalised ``primary_user_email`` on the response (no N+1
    # in the per-row/detail admin views). Plain (non-back_populates) — the User
    # side has no inverse company collection in the MVP.
    primary_user: Mapped[User | None] = relationship("User", lazy="joined")

    # Assigned price list (nullable). Plain (non-back_populates) — the PriceList
    # side has no company collection in the MVP. Loaded on demand; the checkout
    # resolver fetches the list's entries explicitly.
    price_list: Mapped[PriceList | None] = relationship("PriceList", lazy="select")

    # Assigned payment term (nullable). Eager-joined — the net-terms resolver and
    # credit summary both need the term's type/days/due_date, and the admin
    # company response nests it. Plain (non-back_populates); the PaymentTerm side
    # has no company collection.
    payment_term: Mapped[PaymentTerm | None] = relationship("PaymentTerm", lazy="joined")

    # B2B membership rows (Wave D). back_populates the ``CompanyUser.company`` side;
    # cascades on company delete so memberships don't outlive their company.
    members: Mapped[list[CompanyUser]] = relationship(
        "CompanyUser",
        back_populates="company",
        cascade="all, delete-orphan",
    )

    @property
    def primary_user_email(self) -> str | None:
        """Email of the designated primary contact (for the admin response/display).

        Surfaced via Pydantic ``from_attributes``; ``None`` when no contact is set.
        """
        return self.primary_user.email if self.primary_user is not None else None

    def __repr__(self) -> str:
        return f"<Company {self.id} {self.status!r} {self.name!r}>"
