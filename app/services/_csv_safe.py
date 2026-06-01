"""CSV formula-injection neutralizer shared by the CSV exporters.

Spreadsheet apps (Excel / LibreOffice / Google Sheets) treat a cell whose value
begins with ``= + - @`` or a leading tab/CR as a *formula* (CWE-1236, "CSV
injection"). Since several exported columns are user-controlled free text
(order notes, shipping address, customer name, coupon code, product names), an
attacker could plant ``=HYPERLINK(...)`` / ``=cmd|...`` and the admin who opens
the export becomes the victim.

``csv_safe`` prefixes a single quote ``'`` to any string cell that starts with a
dangerous lead character (looking through leading whitespace, which some apps
strip before evaluating). Numeric/date/enum cells are rendered by the callers
and never passed through this — RFC 4180 quoting is unaffected.
"""

from __future__ import annotations

# OWASP CSV-injection lead chars (formula triggers) plus tab/CR.
_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: str) -> str:
    """Neutralize a string cell against spreadsheet formula injection.

    Returns ``value`` unchanged unless it begins (possibly after leading
    whitespace) with a formula-trigger character, in which case a leading ``'``
    is prepended so the spreadsheet treats it as literal text.
    """
    if not value:
        return value
    if value[:1] in _DANGEROUS or value.lstrip()[:1] in _DANGEROUS:
        return "'" + value
    return value
