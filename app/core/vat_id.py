"""EU VAT identification numbers — format handling and parsing.

An EU VAT-ID is a **country prefix + a national number** (e.g. ``PL5252445724``,
``DE811907980``). It is NOT the Polish ``NIP`` stored in ``company.tax_id``: the
NIP is the bare national number, while VIES and the reverse-charge chain need the
prefixed EU form. That is why ``company.vat_id`` is a separate column, not a
widening of ``tax_id``.

Validation here is **format-only** — a well-formed prefix + a plausible body.
Whether the number is *real* is VIES's job (``app/services/vies.py``), never a
regex. Two deliberate wrinkles:

- The VAT prefix is not always the ISO-2 country code: **Greece uses ``EL``**
  (ISO-2 is ``GR``), so the prefix set is its own list, not ``countries.py``.
- Body characters are mostly digits but some members allow letters (IE, NL), so
  the body check is alphanumeric, length 2-12, not digits-only.
"""

import re
from typing import Annotated, Any

from pydantic import AfterValidator, BeforeValidator, Field

# EU/EEA VAT prefixes. ``EL`` (Greece) and ``XI`` (Northern Ireland, post-Brexit
# for goods) are the two that aren't the plain ISO-2 code.
EU_VAT_PREFIXES = frozenset(
    {
        "AT",
        "BE",
        "BG",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "EL",
        "ES",
        "FI",
        "FR",
        "HR",
        "HU",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
        "XI",
    }
)

VAT_ID_PATTERN = r"^[A-Z]{2}[0-9A-Z]{2,12}$"
_VAT_ID_RE = re.compile(VAT_ID_PATTERN)


def normalize_vat_id(value: str) -> str:
    """Upper-case and strip spaces so ``"pl 525 244 57 24"`` becomes ``PL5252445724``.

    Does not validate the prefix — pair with :func:`is_vat_id` (or the schema
    pattern), which rejects what normalisation couldn't rescue.
    """
    return re.sub(r"\s+", "", value).upper()


def is_vat_id(value: str | None) -> bool:
    """True when ``value`` is a well-formed EU VAT-ID with a known prefix."""
    if value is None or not _VAT_ID_RE.match(value):
        return False
    return value[:2] in EU_VAT_PREFIXES


def split_vat_id(value: str) -> tuple[str, str]:
    """Split a normalised VAT-ID into ``(country_prefix, number)``.

    Assumes a valid input (call :func:`is_vat_id` first). VIES's REST API keys on
    the prefix as its ``countryCode`` — ``EL`` and ``XI`` are passed through as-is.
    """
    return value[:2], value[2:]


def _coerce(value: Any) -> Any:
    """Normalize strings; pass anything else through for pydantic to reject."""
    return normalize_vat_id(value) if isinstance(value, str) else value


def _validate_prefix(value: str) -> str:
    if value[:2] not in EU_VAT_PREFIXES:
        raise ValueError(f"'{value[:2]}' is not a recognised EU VAT country prefix")
    return value


EuVatId = Annotated[
    str,
    BeforeValidator(_coerce),
    Field(
        pattern=VAT_ID_PATTERN,
        min_length=4,
        max_length=14,
        description="EU VAT identification number, country-prefixed, e.g. 'PL5252445724'.",
        examples=["PL5252445724"],
    ),
    # Runs on the already-format-valid string, so the prefix slice is safe.
    AfterValidator(_validate_prefix),
]
"""Request-side EU VAT-ID: accepts ``" pl 525 244 57 24 "``, rejects ``"Poland"``."""
