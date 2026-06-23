"""Money helpers — exact 2-decimal arithmetic for currency amounts.

Monetary columns are ``Numeric(12, 2)`` (Postgres ``numeric``), so SQLAlchemy
returns :class:`~decimal.Decimal`. Do all money math in ``Decimal`` and quantize
the result with :func:`quantize_money` rather than the builtin ``round``:

- ``round(Decimal, 2)`` uses banker's rounding (ROUND_HALF_EVEN); the Polish/EU
  VAT convention is ROUND_HALF_UP, so ``2.005 → 2.01``.
- ``round`` on a ``float`` carries binary-float drift into the result.

A 2-decimal scale comfortably covers PLN/USD/EUR; the 12-digit precision allows
amounts up to 9,999,999,999.99 — far beyond any realistic order total.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

#: One grosz / cent — the quantization step for all stored money.
CENTS = Decimal("0.01")

#: Canonical zero amount, already at money scale.
ZERO_MONEY = Decimal("0.00")


def quantize_money(value: Decimal | float | int) -> Decimal:
    """Round ``value`` to 2 decimals using ROUND_HALF_UP (the PL/EU VAT rule).

    Accepts a ``Decimal``, ``float`` or ``int``. A ``float`` is converted via its
    string form so its decimal (not binary) representation is what gets rounded —
    ``Decimal(str(0.1))`` is ``0.1`` exactly, whereas ``Decimal(0.1)`` is not.
    """
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def as_float(value: Decimal | float | None) -> float | None:
    """Coerce a money ``Decimal`` to ``float`` for the JSON wire (None passes through).

    Storage + math are ``Decimal``, but the API wire format is a JSON number so
    the frontend is unchanged. Use this at response-schema construction sites
    that take a money value straight off an ORM row.
    """
    return float(value) if value is not None else None


def to_money(value: Decimal | float | None) -> Decimal | None:
    """Quantize an inbound (float) money value to a storage ``Decimal``; None passes through.

    The mirror of :func:`as_float` — use it when assigning a request-schema
    ``float`` price onto a ``Numeric`` column attribute so the value is stored at
    money scale (and the types line up).
    """
    return quantize_money(value) if value is not None else None
