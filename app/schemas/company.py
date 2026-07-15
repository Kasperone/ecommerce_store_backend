"""Pydantic schemas for admin company management.

``CompanyStatusLiteral`` is hardcoded (pyright needs literal arguments in type
positions) but cross-checked against the model's ``COMPANY_STATUS_VALUES`` at
import time, so the DB CHECK, the schema, and the route can't drift apart
silently. Mirrors ``CampaignStatusLiteral`` / ``RefundReasonLiteral``.

``status`` is constrained to its allowed set by the literal; there is no
cross-field validation (a company is a flat directory entry). An approval
workflow beyond the status field, and linking users to companies, are out of
scope, so there is no "approve" schema and no user-link field.

``CompanyResponse`` exposes at least ``{ id, name, status }`` — the stable
contract the quotes vertical depends on (``quotes.company_id -> companies.id``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.models.company import COMPANY_STATUS_VALUES
from app.models.company_user import COMPANY_MEMBER_STATUS_VALUES, COMPANY_ROLE_VALUES
from app.schemas.base import BaseSchema, TimestampSchema

# Hardcoded for pyright; the cross-check below catches drift from the model.
CompanyStatusLiteral = Literal["pending", "approved", "rejected"]

# Import-time drift catch: a status added to one side without the other fails
# the import (and so every dependent route/test) with a clear message.
assert set(CompanyStatusLiteral.__args__) == set(COMPANY_STATUS_VALUES), (  # noqa: S101
    "CompanyStatusLiteral drifted from app.models.company.COMPANY_STATUS_VALUES: "
    f"schema={set(CompanyStatusLiteral.__args__)} vs model={set(COMPANY_STATUS_VALUES)}"
)

# The caller's B2B role on ``GET /companies/me`` (Wave D). Hardcoded for pyright;
# cross-checked against the model's value set at import time (mirrors the status
# literal above) so the schema, DB CHECK, and FE role helper can't drift.
CompanyRoleLiteral = Literal["COMPANY_ADMIN", "BUYER", "APPROVER", "VIEWER"]

assert set(CompanyRoleLiteral.__args__) == set(COMPANY_ROLE_VALUES), (  # noqa: S101
    "CompanyRoleLiteral drifted from app.models.company_user.COMPANY_ROLE_VALUES: "
    f"schema={set(CompanyRoleLiteral.__args__)} vs model={set(COMPANY_ROLE_VALUES)}"
)


class CompanyBase(BaseSchema):
    """Shared company fields (creation + response).

    ``name`` is required and trimmed; everything else is optional. ``status``
    defaults to ``pending``.
    """

    name: Annotated[str, Field(min_length=1, max_length=255)]
    tax_id: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    status: CompanyStatusLiteral = "pending"
    account_manager: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=5000)
    # Designated primary contact customer (the user a quote→order conversion
    # attributes the resulting order to). Optional; the route validates the
    # referenced user exists (422 if not).
    primary_user_id: int | None = Field(default=None)
    # B2B company profile fields (Wave D / D-D-4). All optional; surfaced on the
    # response (so ``GET /companies/me`` carries them) and editable by a
    # COMPANY_ADMIN via ``PATCH /companies/me`` (see ``CompanyProfileUpdate``).
    regon: str | None = Field(default=None, max_length=32)
    billing_address_street: str | None = Field(default=None, max_length=255)
    billing_address_city: str | None = Field(default=None, max_length=128)
    billing_address_postal_code: str | None = Field(default=None, max_length=32)
    billing_address_country: str | None = Field(default=None, max_length=128)
    billing_address_state: str | None = Field(default=None, max_length=128)
    default_currency: str | None = Field(default=None, max_length=3)
    # Assigned B2B price list (admin-set). Null = catalog pricing. Surfaced on the
    # response so the admin directory + company member can see the assignment;
    # only an admin can change it (via ``CompanyUpdate`` — the customer self-edit
    # schema ``CompanyProfileUpdate`` does not include it).
    price_list_id: int | None = Field(default=None)

    @model_validator(mode="after")
    def _normalise_name(self) -> CompanyBase:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("name must not be blank")
        return self


class CompanyCreate(CompanyBase):
    """Request body for ``POST /api/v1/admin/companies``."""


class CompanyUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/admin/companies/{id}`` (all fields optional).

    Only fields explicitly sent are applied. ``status`` stays constrained to the
    allowed set by the literal.
    """

    name: Annotated[str | None, Field(min_length=1, max_length=255)] = None
    tax_id: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    status: CompanyStatusLiteral | None = None
    account_manager: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=5000)
    # Set/clear the primary contact. Sending ``null`` explicitly clears it; omit to
    # leave untouched (the route validates a non-null value references a real user).
    primary_user_id: int | None = Field(default=None)
    # Assign/clear the B2B price list. Sending ``null`` clears it (→ catalog
    # pricing); omit to leave untouched. The route validates a non-null value
    # references an existing price list (422 if not).
    price_list_id: int | None = Field(default=None)

    @model_validator(mode="after")
    def _normalise_name(self) -> CompanyUpdate:
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("name must not be blank")
        return self


class CompanyResponse(CompanyBase, TimestampSchema):
    """A company row as the API returns it.

    Includes at least ``{ id, name, status }`` (id + timestamps come from
    ``TimestampSchema``) — the stable contract the quotes vertical FKs against.
    ``primary_user_email`` is a read-only denormalised display value (the
    designated contact's email, or ``null`` when none is set).
    """

    primary_user_email: str | None = None


class CompanyListResponse(BaseSchema):
    """Paginated company list."""

    items: list[CompanyResponse]
    total: int
    page: int
    page_size: int
    pages: int


class CompanyMeResponse(CompanyResponse):
    """The caller's own company on ``GET /companies/me`` (Wave D).

    Extends ``CompanyResponse`` with the caller's B2B ``role`` so the frontend can
    role-gate the B2B surfaces (VIEWER read-only; BUYER request quotes + checkout;
    COMPANY_ADMIN profile + team). Built from a ``CompanyMembership`` (the company
    fields come from ``membership.company`` via ``from_attributes``; ``role`` is
    set explicitly by the route)."""

    role: CompanyRoleLiteral


# ===========================================================================
# CP-be APPEND BLOCK — company-profile self-service (Wave D / D-D-4).
# Shared file with TM-be; additions below are appended, no edits above.
# ===========================================================================


class CompanyProfileUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/companies/me`` — the COMPANY_ADMIN-editable
    company-profile fields (Wave D / D-D-4).

    Deliberately a **narrow** allow-list: a COMPANY_ADMIN may edit the company's
    own contact + billing profile, but NOT ``name`` / ``status`` /
    ``account_manager`` / ``primary_user_id`` (those are admin-directory concerns
    — ``status`` is the approval gate; the primary contact + account manager are
    set by staff). All fields are optional; only those explicitly sent are
    applied (``exclude_unset`` in the CRUD). ``default_currency`` is normalised to
    upper-case; an empty/blank string for any field clears it to ``None``."""

    tax_id: str | None = Field(default=None, max_length=64)
    regon: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    billing_address_street: str | None = Field(default=None, max_length=255)
    billing_address_city: str | None = Field(default=None, max_length=128)
    billing_address_postal_code: str | None = Field(default=None, max_length=32)
    billing_address_country: str | None = Field(default=None, max_length=128)
    billing_address_state: str | None = Field(default=None, max_length=128)
    default_currency: Annotated[str | None, Field(default=None, max_length=3)] = None

    @model_validator(mode="after")
    def _normalise(self) -> CompanyProfileUpdate:
        # Trim every string field; an empty result becomes ``None`` (an explicit
        # clear). ``default_currency`` is upper-cased (ISO 4217 is upper-case).
        for field in (
            "tax_id",
            "regon",
            "email",
            "phone",
            "billing_address_street",
            "billing_address_city",
            "billing_address_postal_code",
            "billing_address_country",
            "billing_address_state",
            "default_currency",
        ):
            value = getattr(self, field)
            if isinstance(value, str):
                trimmed = value.strip()
                setattr(self, field, trimmed or None)
        if self.default_currency:
            self.default_currency = self.default_currency.upper()
        return self


# TM-be (Wave D slice 5) — team management on /api/v1/companies/me/users.
# Appended block (CP-be also appends to this module — keep additions grouped
# and labelled so the parallel merge is a clean concat). Shares the
# CompanyRoleLiteral above with CU-be/CP-be.
# ===========================================================================

# Membership lifecycle status on the team API. Hardcoded for pyright; cross-
# checked against the model value set at import time (mirrors the role literal).
CompanyMemberStatusLiteral = Literal["active", "invited", "suspended"]

assert set(CompanyMemberStatusLiteral.__args__) == set(COMPANY_MEMBER_STATUS_VALUES), (  # noqa: S101
    "CompanyMemberStatusLiteral drifted from "
    "app.models.company_user.COMPANY_MEMBER_STATUS_VALUES: "
    f"schema={set(CompanyMemberStatusLiteral.__args__)} "
    f"vs model={set(COMPANY_MEMBER_STATUS_VALUES)}"
)


class CompanyMemberResponse(TimestampSchema):
    """A ``company_users`` row as the team API returns it.

    ``user_id`` is the linked account, or ``None`` for a **pending email-invite**
    to an address with no account yet (bound when that address registers).
    ``user_email`` / ``user_name`` are read-only denormalised display values from
    the linked ``User`` (resolved by the route so the FE roster doesn't need a
    second fetch; ``None`` for a pending invite). ``invited_email`` / ``invited_at``
    are populated for rows created via an invite, ``None`` otherwise.
    """

    company_id: int
    user_id: int | None = None
    role: CompanyRoleLiteral
    status: CompanyMemberStatusLiteral
    invited_email: str | None = None
    invited_at: datetime | None = None
    user_email: str | None = None
    user_name: str | None = None


class CompanyMemberInvite(BaseSchema):
    """Request body for ``POST /api/v1/companies/me/users`` (invite by email + role).

    ``email`` must resolve to an existing user account (the membership links to it);
    ``role`` is the B2B role to grant. ``COMPANY_ADMIN`` is invitable (a company can
    have several admins). The route creates an ``invited`` membership.
    """

    email: Annotated[str, Field(min_length=3, max_length=255)]
    role: CompanyRoleLiteral

    @model_validator(mode="after")
    def _normalise_email(self) -> CompanyMemberInvite:
        self.email = self.email.strip().lower()
        if not self.email:
            raise ValueError("email must not be blank")
        return self


class CompanyMemberUpdate(BaseSchema):
    """Request body for ``PATCH /api/v1/companies/me/users/{id}`` (role / status).

    Both fields optional — a PATCH may change just the role (e.g. promote to
    COMPANY_ADMIN) or just the status (e.g. ``suspended`` to deactivate, ``active``
    to reactivate). At least one must be provided (else the PATCH is a no-op → 422).
    The last-COMPANY_ADMIN guard lives in the route.
    """

    role: CompanyRoleLiteral | None = None
    status: CompanyMemberStatusLiteral | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> CompanyMemberUpdate:
        if self.role is None and self.status is None:
            raise ValueError("at least one of role / status must be provided")
        return self
