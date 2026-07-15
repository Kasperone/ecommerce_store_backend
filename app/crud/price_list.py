"""Price-list CRUD — admin-managed B2B negotiated pricing (Shopify-gap P2 #14).

Two responsibilities:

1. **Management** (mirrors ``CRUDCompany`` / ``CRUDCoupon``: no commit anywhere,
   the route owns the transaction): CRUD the list header and its entries.
2. **Resolution** (the checkout + storefront read path): ``resolve_entry_map``
   builds a ``(product_id, variant_id|None) -> PriceListEntry`` lookup for a
   list, and ``price_for`` applies the grain precedence (variant-grain wins,
   then product-grain) + per-currency fallback (an entry lacking the shopper's
   currency returns ``None`` so the caller keeps the catalog price — no
   cross-currency fallback).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session, selectinload

from app.core.money import to_money
from app.models.company import Company
from app.models.price_list import PriceList, PriceListEntry
from app.schemas.price_list import (
    PriceListCreate,
    PriceListEntryCreate,
    PriceListEntryUpdate,
    PriceListUpdate,
)


class CRUDPriceList:
    """Price-list rows + entries (no commit; caller owns the txn)."""

    # --- header management -------------------------------------------------

    def get(self, db: Session, id: int) -> PriceList | None:
        return db.get(PriceList, id)

    def get_with_entries(self, db: Session, id: int) -> PriceList | None:
        """A list with its entries eager-loaded (+ product/variant for names)."""
        return (
            db.query(PriceList)
            .options(
                selectinload(PriceList.entries).selectinload(PriceListEntry.product),
                selectinload(PriceList.entries).selectinload(PriceListEntry.variant),
            )
            .filter(PriceList.id == id)
            .first()
        )

    def get_multi(
        self, db: Session, *, search: str | None = None, skip: int = 0, limit: int = 100
    ) -> list[PriceList]:
        query = self._filtered(db, search=search)
        return (
            query.order_by(PriceList.created_at.desc(), PriceList.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count(self, db: Session, *, search: str | None = None) -> int:
        return self._filtered(db, search=search).count()

    def _filtered(self, db: Session, *, search: str | None):
        query = db.query(PriceList)
        if search:
            query = query.filter(PriceList.name.ilike(f"%{search}%"))
        return query

    def create(self, db: Session, *, obj_in: PriceListCreate) -> PriceList:
        row = PriceList(name=obj_in.name, is_active=obj_in.is_active, notes=obj_in.notes)
        db.add(row)
        db.flush()
        return row

    def update(self, db: Session, *, db_obj: PriceList, obj_in: PriceListUpdate) -> PriceList:
        for field, value in obj_in.model_dump(exclude_unset=True).items():
            setattr(db_obj, field, value)
        db.flush()
        return db_obj

    def delete(self, db: Session, *, db_obj: PriceList) -> None:
        db.delete(db_obj)
        db.flush()

    def in_use_count(self, db: Session, *, price_list_id: int) -> int:
        """How many companies are assigned this list (for a delete-safety note)."""
        return db.query(Company).filter(Company.price_list_id == price_list_id).count()

    # --- entry management --------------------------------------------------

    def get_entry(self, db: Session, id: int) -> PriceListEntry | None:
        return db.get(PriceListEntry, id)

    def find_entry(
        self, db: Session, *, price_list_id: int, product_id: int, variant_id: int | None
    ) -> PriceListEntry | None:
        """The existing entry at this exact grain, if any (for upsert / dup guard)."""
        return (
            db.query(PriceListEntry)
            .filter(
                PriceListEntry.price_list_id == price_list_id,
                PriceListEntry.product_id == product_id,
                PriceListEntry.variant_id.is_(None)
                if variant_id is None
                else PriceListEntry.variant_id == variant_id,
            )
            .first()
        )

    def add_entry(
        self, db: Session, *, price_list_id: int, obj_in: PriceListEntryCreate
    ) -> PriceListEntry:
        row = PriceListEntry(
            price_list_id=price_list_id,
            product_id=obj_in.product_id,
            variant_id=obj_in.variant_id,
            price_usd=to_money(obj_in.price_usd),
            price_pln=to_money(obj_in.price_pln),
            price_eur=to_money(obj_in.price_eur),
        )
        db.add(row)
        db.flush()
        return row

    def update_entry(
        self, db: Session, *, db_obj: PriceListEntry, obj_in: PriceListEntryUpdate
    ) -> PriceListEntry:
        data = obj_in.model_dump(exclude_unset=True)
        for field in ("price_usd", "price_pln", "price_eur"):
            if field in data:
                setattr(db_obj, field, to_money(data[field]))
        db.flush()
        return db_obj

    def delete_entry(self, db: Session, *, db_obj: PriceListEntry) -> None:
        db.delete(db_obj)
        db.flush()

    # --- resolution (checkout + storefront read) ---------------------------

    def resolve_entry_map(
        self, db: Session, *, price_list_id: int
    ) -> dict[tuple[int, int | None], PriceListEntry]:
        """Build a ``(product_id, variant_id) -> entry`` lookup for one list.

        ``variant_id`` NULL is the product-grain key. One query; the caller then
        prices each order/display line via ``price_for``.
        """
        rows = db.query(PriceListEntry).filter(PriceListEntry.price_list_id == price_list_id).all()
        return {(row.product_id, row.variant_id): row for row in rows}

    def price_for(
        self,
        entry_map: dict[tuple[int, int | None], PriceListEntry],
        *,
        product_id: int,
        variant_id: int | None,
        currency: str,
    ) -> Decimal | None:
        """Resolve the negotiated price for a line, or ``None`` (→ keep catalog).

        Grain precedence: a variant-grain entry (``variant_id`` set) wins over
        the product-grain entry for that variant. Per-currency: an entry lacking
        the shopper's currency yields ``None`` (no cross-currency fallback — the
        caller keeps the catalog price in that currency).
        """
        entry: PriceListEntry | None = None
        if variant_id is not None:
            entry = entry_map.get((product_id, variant_id))
        if entry is None:
            entry = entry_map.get((product_id, None))
        if entry is None:
            return None
        return entry.get_price(currency)


price_list = CRUDPriceList()
