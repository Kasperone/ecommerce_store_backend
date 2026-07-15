"""Shared order/draft total recomputation.

The single arithmetic seam used by both draft-order edits
(``crud_draft_order``) and real pending-order edits (``crud_order.edit_items``),
so the money math lives in exactly one place. Deliberately narrow: it takes
already-resolved line totals + the discount + shipping and returns
``(subtotal, total)`` as exact ``Decimal`` (ROUND_HALF_UP via ``money``).

VAT is NOT computed here — the project stores prices VAT-inclusive and derives
the per-rate faktura decomposition at invoice time (``services/invoice.decompose_gross``
on ``order.total``); gift-card / store-credit tenders are a snapshot on the
order and feed ``amount_due`` (a derived property), not the total. So the total
formula is the documented ``total == quantize_money(subtotal - discount +
shipping)`` (clamped at 0). Drafts pass ``shipping=0`` (shipping isn't part of a
draft's total).
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from app.core.money import ZERO_MONEY, quantize_money


def recompute_totals(
    *,
    line_totals: Iterable[Decimal | None],
    discount: Decimal,
    shipping: Decimal = ZERO_MONEY,
) -> tuple[Decimal, Decimal]:
    """Return ``(subtotal, total)`` from resolved line totals + discount + shipping.

    A ``None`` line total (a stale draft line — product deleted, no override)
    contributes 0. ``subtotal`` is the quantized sum; ``total = max(subtotal -
    discount + shipping, 0)`` quantized.
    """
    subtotal = ZERO_MONEY
    for line_total in line_totals:
        if line_total is not None:
            subtotal += line_total
    subtotal = quantize_money(subtotal)
    total = max(quantize_money(subtotal - discount + shipping), ZERO_MONEY)
    return subtotal, total
