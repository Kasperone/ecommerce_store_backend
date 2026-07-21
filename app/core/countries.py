"""ISO 3166-1 alpha-2 country handling.

Addresses are stored as two-letter uppercase codes, never country *names*.
Everything downstream keys off the code — shipping-zone matching
(``shipping_zone_countries.country_code``) today, destination VAT resolution
tomorrow — so a free-text ``"Poland"`` doesn't just look untidy, it silently
fails to match a zone and would silently mis-resolve tax.

Validation here is deliberately **format-only** (same rule as the zone column),
not membership in a canonical 249-entry list: the store's own zone table is the
authority on which countries it actually ships to, and a hardcoded list is one
more thing to keep current when the ISO register changes.
"""

import re
from typing import Annotated, Any

from pydantic import BeforeValidator, Field

ISO2_PATTERN = r"^[A-Z]{2}$"

_ISO2_RE = re.compile(ISO2_PATTERN)


def normalize_country_code(value: str) -> str:
    """Strip and upper-case a country code so ``" pl "`` is accepted as ``"PL"``.

    Does not validate — pair with the schema's ``pattern=ISO2_PATTERN``, which
    rejects whatever this couldn't rescue. Lower-case input is a normal thing
    for a browser to send; a country *name* is not, and stays an error.
    """
    return value.strip().upper()


def is_iso2(value: str | None) -> bool:
    """True when ``value`` is a well-formed uppercase alpha-2 code."""
    return value is not None and _ISO2_RE.match(value) is not None


def _coerce(value: Any) -> Any:
    """Normalize strings, pass anything else through for pydantic to reject."""
    return normalize_country_code(value) if isinstance(value, str) else value


CountryCode = Annotated[
    str,
    BeforeValidator(_coerce),
    Field(
        pattern=ISO2_PATTERN,
        min_length=2,
        max_length=2,
        description="ISO 3166-1 alpha-2 country code, e.g. 'PL'.",
        examples=["PL"],
    ),
]
"""Request-side country code: accepts ``" pl "``, rejects ``"Poland"``."""
