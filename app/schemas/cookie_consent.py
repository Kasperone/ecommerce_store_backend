"""Pydantic schemas for the public cookie-consent endpoint.

``CookieConsentCategories`` models the per-category booleans the FE banner sends.
``necessary`` is coerced ``True`` server-side at record time (strictly-necessary
cookies are exempt from consent and must never be refusable) — the validator here
defends the boundary, the CRUD layer re-coerces as belt-and-braces.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.schemas.base import BaseSchema


class CookieConsentCategories(BaseSchema):
    """Per-category cookie consent booleans. ``necessary`` is always true."""

    necessary: bool = True
    analytics: bool = False
    marketing: bool = False

    @field_validator("necessary")
    @classmethod
    def _necessary_always_true(cls, _value: bool) -> bool:
        # Strictly-necessary cookies are exempt from consent — never refusable.
        return True


class CookieConsentRequest(BaseSchema):
    """Payload for recording a cookie-consent choice (guest or authed)."""

    anon_id: str | None = Field(
        default=None,
        max_length=64,
        description="FE client-id cookie identifying the visitor (guests).",
    )
    categories: CookieConsentCategories = Field(default_factory=CookieConsentCategories)
    gpc: bool = Field(
        default=False, description="Global Privacy Control signal sent by the client."
    )
    policy_version: str | None = Field(
        default=None, max_length=50, description="Cookie-policy version in force."
    )


class CookieConsentResponse(BaseSchema):
    """The current cookie-consent choice (latest row), or null fields if none."""

    categories: CookieConsentCategories
    gpc: bool
    policy_version: str | None
    created_at: datetime
