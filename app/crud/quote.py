"""Quote CRUD — admin-managed B2B price quotes (header + line items).

Mirrors ``CRUDCompany`` for the per-resource shape and ``crud_order.create_with_items``
for the header+items create: no commit anywhere (the route owns the transaction
so the mutation and its audit row land together).

The **server computes** every ``line_total`` (= ``quantity * unit_price``) plus
the quote ``subtotal`` / ``total`` (``total == subtotal`` in MVP — no tax /
discount). The client never supplies totals. ``product_id`` on a line is an
optional, un-validated catalog link (SET NULL on product delete) — quotes can
reference free-text items that aren't catalog products, so a missing product is
not an error (mirrors the deferred, lenient posture of the MVP).

**Manage-only.** Quote→order conversion and any customer-facing flow are out of
scope and live elsewhere (when they exist).
"""

from __future__ import annotations

from sqlalchemy.orm import Query, Session, selectinload

from app.models.company import Company
from app.models.quote import Quote, QuoteItem
from app.schemas.quote import QuoteCreate, QuoteItemCreate, QuoteUpdate


def _build_items(lines: list[QuoteItemCreate]) -> tuple[list[QuoteItem], float]:
    """Build ``QuoteItem`` rows with computed ``line_total`` + return the subtotal.

    ``line_total = round(quantity * unit_price, 2)``; the subtotal is the rounded
    sum of the line totals. Pure — no DB access (``product_id`` is stored as-is,
    not validated).
    """
    items: list[QuoteItem] = []
    subtotal = 0.0
    for line in lines:
        line_total = round(line.quantity * line.unit_price, 2)
        subtotal += line_total
        items.append(
            QuoteItem(
                product_id=line.product_id,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                line_total=line_total,
            )
        )
    return items, round(subtotal, 2)


class CRUDQuote:
    """Quote rows + nested items (no commit; caller owns the txn)."""

    def get(self, db: Session, id: int) -> Quote | None:
        """Fetch one quote with its company + items eager-loaded (no N+1)."""
        return (
            db.query(Quote)
            .options(selectinload(Quote.company), selectinload(Quote.items))
            .filter(Quote.id == id)
            .first()
        )

    def company_exists(self, db: Session, company_id: int) -> bool:
        """True if a company with ``company_id`` exists (route 404s on False)."""
        return db.query(Company.id).filter(Company.id == company_id).first() is not None

    def get_multi(
        self,
        db: Session,
        *,
        status: str | None = None,
        company_id: int | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[Quote]:
        query = self._filtered(db, status=status, company_id=company_id)
        return (
            query.options(selectinload(Quote.company), selectinload(Quote.items))
            .order_by(Quote.created_at.desc(), Quote.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def count(
        self, db: Session, *, status: str | None = None, company_id: int | None = None
    ) -> int:
        return self._filtered(db, status=status, company_id=company_id).count()

    def _filtered(self, db: Session, *, status: str | None, company_id: int | None) -> Query[Quote]:
        query = db.query(Quote)
        if status is not None:
            query = query.filter(Quote.status == status)
        if company_id is not None:
            query = query.filter(Quote.company_id == company_id)
        return query

    def create_with_items(self, db: Session, *, obj_in: QuoteCreate) -> Quote:
        """Create a quote from posted line items.

        Computes each ``line_total`` + the ``subtotal`` / ``total`` server-side.
        Flushes so the ``id`` is populated; does not commit. The caller checks
        the company exists first (``company_exists``) and 404s on miss.
        """
        items, subtotal = _build_items(obj_in.items)
        quote = Quote(
            company_id=obj_in.company_id,
            status="draft",
            currency=obj_in.currency,
            valid_until=obj_in.valid_until,
            notes=obj_in.notes,
            subtotal=subtotal,
            total=subtotal,  # total == subtotal in MVP
            items=items,
        )
        db.add(quote)
        db.flush()
        db.refresh(quote)
        return quote

    def update(self, db: Session, *, quote: Quote, obj_in: QuoteUpdate) -> Quote:
        """Apply a partial update (no flush — the audit writer reads ``get_history``
        between this and the caller's commit).

        Scalar fields (``status`` / ``currency`` / ``valid_until`` / ``notes``)
        are set only when explicitly sent. If ``items`` is provided, the whole
        line set is replaced (``delete-orphan`` removes the old rows) and the
        ``subtotal`` / ``total`` are recomputed; if ``items`` is omitted the lines
        and totals are left untouched.
        """
        data = obj_in.model_dump(exclude_unset=True)
        for field in ("status", "currency", "valid_until", "notes"):
            if field in data:
                setattr(quote, field, data[field])
        if obj_in.items is not None:
            items, subtotal = _build_items(obj_in.items)
            quote.items = items
            quote.subtotal = subtotal
            quote.total = subtotal  # total == subtotal in MVP
        db.add(quote)
        return quote

    def delete(self, db: Session, *, quote: Quote) -> None:
        """Delete a quote; its items cascade (FK ON DELETE CASCADE + delete-orphan)."""
        db.delete(quote)


quote = CRUDQuote()
