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

from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.models.company import COMPANY_STATUS_VALUES
from app.models.company_user import COMPANY_ROLE_VALUES
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
