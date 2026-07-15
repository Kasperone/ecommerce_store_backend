"""B2B price-list resolution for a shopping user (Shopify-gap P2 #14).

The single choke point that answers: "for this authenticated user, what
negotiated prices apply?" Both the money-path (checkout, ``create_with_items``)
and the storefront display path (product reads) go through
``resolve_active_entry_map`` so they can never diverge.

A user gets negotiated pricing iff: they have an **active** company membership
(``crud_company_user.get_active_membership``), their company has a
``price_list_id``, and that price list is **active**. Otherwise ``None`` — the
caller keeps catalog prices everywhere. Guests (``user_id is None``) always get
``None``.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from app import crud
from app.models.price_list import PriceListEntry
from app.models.product import Product


def resolve_active_entry_map(
    db: Session, *, user_id: int | None
) -> dict[tuple[int, int | None], PriceListEntry] | None:
    """The ``(product_id, variant_id) -> entry`` map for ``user_id``, or ``None``.

    ``None`` means "no negotiated pricing applies" (guest, non-B2B account,
    company with no list, or an inactive list). An empty dict is possible for an
    active-but-entryless list — the caller then keeps catalog everywhere, which
    is the same net effect as ``None`` but distinguishes "list exists, no entry
    for this product" cleanly.
    """
    if user_id is None:
        return None
    membership = crud.company_user.get_active_membership(db, user_id=user_id)
    if membership is None:
        return None
    price_list_id = membership.company.price_list_id
    if price_list_id is None:
        return None
    price_list = crud.price_list.get(db, price_list_id)
    if price_list is None or not price_list.is_active:
        return None
    return crud.price_list.resolve_entry_map(db, price_list_id=price_list_id)


def annotate_products_for_user(
    db: Session, *, products: Iterable[Product], user_id: int | None
) -> None:
    """Set ``b2b_price_usd/pln/eur`` on each product for a B2B caller (in place).

    Reads the caller's active price list once and stamps each product's
    **product-grain** negotiated price (the three currency values) onto transient
    attributes the ``ProductResponse`` surfaces. For guests / non-B2B callers /
    products with no product-grain entry, the three fields are explicitly set to
    ``None`` — the helper ALWAYS writes so a stamp from a prior call on the same
    ORM instance (possible when a session is reused across requests) can never
    leak. Variant-grain entries are intentionally not surfaced here — they still
    apply at checkout.
    """
    entry_map = resolve_active_entry_map(db, user_id=user_id)
    for product in products:
        entry = entry_map.get((product.id, None)) if entry_map else None
        # Shadow the ClassVar transient in the instance __dict__ (getattr /
        # Pydantic from_attributes read it there first). Writing __dict__ keeps
        # both linters happy — direct assignment trips pyright's assign-to-
        # classvar check, and setattr with a literal name trips ruff B010.
        product.__dict__["b2b_price_usd"] = entry.price_usd if entry else None
        product.__dict__["b2b_price_pln"] = entry.price_pln if entry else None
        product.__dict__["b2b_price_eur"] = entry.price_eur if entry else None
