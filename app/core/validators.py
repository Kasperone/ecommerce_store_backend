"""Reusable domain validators.

Currently the Polish **NIP** (tax identification number) used on a faktura. A
NIP is 10 digits with a weighted mod-11 checksum, so a typo'd NIP can be caught
client- and server-side before it lands on a legal invoice.
"""

from __future__ import annotations

import re

# Official NIP checksum weights for the first 9 digits (the 10th is the check).
_NIP_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)
_NIP_DIGITS = re.compile(r"\D")


def normalize_nip(nip: str) -> str:
    """Strip everything but digits (NIPs are often written ``123-456-32-18`` or
    with a ``PL`` prefix)."""
    return _NIP_DIGITS.sub("", nip)


def is_valid_nip(nip: str) -> bool:
    """True if ``nip`` is a structurally valid Polish NIP (10 digits + checksum).

    The check digit is ``sum(dᵢ·wᵢ) mod 11``; a remainder of 10 can never be a
    valid NIP (no single check digit represents it), so such numbers are
    rejected. Formatting (dashes, spaces, a ``PL`` prefix) is ignored.
    """
    digits = normalize_nip(nip)
    if len(digits) != 10 or not digits.isdigit():
        return False
    checksum = sum(int(digits[i]) * _NIP_WEIGHTS[i] for i in range(9)) % 11
    if checksum == 10:
        return False
    return checksum == int(digits[9])
