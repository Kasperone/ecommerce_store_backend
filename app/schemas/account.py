"""Schemas for the self-service account preferences endpoints (`/me/preferences`).

Kept separate from `app.schemas.user` so the hand-rolled FE `UserResponse` type
stays untouched — these get a clean OpenAPI shape of their own (see decision D-A-2).
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.base import BaseSchema
from app.schemas.user import Locale

# The supported storefront currencies. Mirrors the literal used in
# ``app.schemas.coupon`` / ``app.schemas.quote`` — kept in sync deliberately.
CurrencyLiteral = Literal["USD", "PLN", "EUR"]


class PreferencesResponse(BaseSchema):
    """The caller's own preferences snapshot (shape shared by GET + PATCH).

    ``consents`` is the latest-per-type map computed from the append-only
    ``user_consents`` log (e.g. ``{"marketing_email": true}``); types the user has
    never touched are simply absent. Currency/locale unchanged from Wave A.
    """

    preferred_currency: CurrencyLiteral | None = None
    locale: Locale
    consents: dict[str, bool] = Field(default_factory=dict)


class PreferencesUpdate(BaseModel):
    """Partial update of the caller's preferences.

    Only fields explicitly present in the request body are applied. Distinguishing
    "absent" from an explicit ``null`` matters: ``preferred_currency: null`` clears
    the preference, while omitting it leaves the stored value unchanged. The route
    inspects ``model_fields_set`` to tell the two apart.

    ``consents`` is an optional partial map of ``{consent_type: granted}``; each
    provided entry appends a new row to the append-only consent log (never an
    UPDATE). Omitting it leaves the consent state untouched.
    """

    # ``populate_by_name`` only; intentionally NOT ``from_attributes`` — this is an
    # input schema, never built from an ORM object.
    model_config = ConfigDict(populate_by_name=True)

    preferred_currency: CurrencyLiteral | None = None
    locale: Locale | None = None
    consents: dict[str, bool] | None = None


class DataExportResponse(BaseModel):
    """GDPR Art. 15/20 export — the caller's full personal-data snapshot.

    Loosely typed (``dict``/``list[dict]``) on purpose: this is a
    machine-readable dump for the data subject, not a long-lived API contract, so
    it can carry whatever columns the source rows expose without a schema lockstep.
    """

    user: dict[str, Any]
    addresses: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    consents: list[dict[str, Any]]


class AccountDeletionResponse(BaseModel):
    """Acknowledgement of an account-deletion request (Art. 17 soft-delete)."""

    deletion_requested_at: datetime
    anonymized_at: datetime | None = None
    detail: str = "Your account has been anonymized."
