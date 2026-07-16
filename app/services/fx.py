"""Currency conversion for B2B cross-currency net-terms credit (tail Program D).

The store normally stores prices per-currency and never converts at runtime
(see the root CLAUDE.md decision). Cross-currency **credit** is the one exception
the user opted into: a company's credit limit is denominated in its
``default_currency``, but its members may place net-terms orders in another
currency, so the credit check must convert those orders into the limit's currency.

Rates are **admin-configured** (``store_config.fx_rates``), not fetched live — a
dependency-free source the merchant keeps current (staleness is the merchant's
responsibility, per the user's decision). ``fx_rates`` maps each ISO-4217 code to
the value of **1 unit of that currency in a common pivot unit** (e.g. relative to
USD: ``{"USD": 1, "EUR": 1.08, "PLN": 0.25}``). Converting A→B divides out the
pivot: ``amount_B = amount_A * rate[A] / rate[B]``.

``convert_money`` returns ``None`` when a rate is missing (the caller then treats
the currencies as incomparable — the pre-Program-D behavior, i.e. reject the
pay-on-account option for that currency). Same-currency conversion is the
identity and never needs a configured rate.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.money import quantize_money

Rates = dict[str, float]


def convert_money(
    amount: Decimal, from_ccy: str, to_ccy: str, rates: Rates | None
) -> Decimal | None:
    """Convert ``amount`` from ``from_ccy`` to ``to_ccy`` via admin FX ``rates``.

    Same-currency is the identity (no rate needed). Cross-currency needs a
    positive rate for both sides; a missing/zero rate returns ``None`` (the
    currencies are treated as incomparable). Result is quantized money (HALF_UP).
    """
    if from_ccy == to_ccy:
        return quantize_money(amount)
    rates = rates or {}
    r_from = rates.get(from_ccy)
    r_to = rates.get(to_ccy)
    if r_from is None or r_to is None:
        return None
    r_from_d = Decimal(str(r_from))
    r_to_d = Decimal(str(r_to))
    if r_from_d <= 0 or r_to_d <= 0:
        return None
    return quantize_money(Decimal(str(amount)) * r_from_d / r_to_d)
