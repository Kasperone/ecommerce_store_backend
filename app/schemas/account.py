"""Schemas for the self-service account preferences endpoints (`/me/preferences`).

Kept separate from `app.schemas.user` so the hand-rolled FE `UserResponse` type
stays untouched — these get a clean OpenAPI shape of their own (see decision D-A-2).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.base import BaseSchema
from app.schemas.user import Locale

# The supported storefront currencies. Mirrors the literal used in
# ``app.schemas.coupon`` / ``app.schemas.quote`` — kept in sync deliberately.
CurrencyLiteral = Literal["USD", "PLN", "EUR"]


class PreferencesResponse(BaseSchema):
    """The caller's own preferences snapshot (shape shared by GET + PATCH)."""

    preferred_currency: CurrencyLiteral | None = None
    locale: Locale


class PreferencesUpdate(BaseModel):
    """Partial update of the caller's preferences.

    Only fields explicitly present in the request body are applied. Distinguishing
    "absent" from an explicit ``null`` matters: ``preferred_currency: null`` clears
    the preference, while omitting it leaves the stored value unchanged. The route
    inspects ``model_fields_set`` to tell the two apart.
    """

    # ``populate_by_name`` only; intentionally NOT ``from_attributes`` — this is an
    # input schema, never built from an ORM object.
    model_config = ConfigDict(populate_by_name=True)

    preferred_currency: CurrencyLiteral | None = None
    locale: Locale | None = None
