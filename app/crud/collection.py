"""CRUD for manual collections + the collection↔product membership join.

Mirrors ``app/crud/category.py`` for slug resolution + active listing, and adds
membership management over the ``collection_products`` join: an ordered-set
replace (``set_products``) plus single add/remove. Mutating methods stage on the
session but never commit — the route owns the transaction boundary.
"""

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.collection import Collection, collection_products
from app.schemas.collection import CollectionCreate, CollectionUpdate


class CRUDCollection(CRUDBase[Collection, CollectionCreate, CollectionUpdate]):
    """CRUD operations for the Collection model."""

    def get_by_slug(self, db: Session, *, slug: str) -> Collection | None:
        return db.query(Collection).filter(Collection.slug == slug).first()

    def get_by_slug_pl(self, db: Session, *, slug_pl: str) -> Collection | None:
        return db.query(Collection).filter(Collection.slug_pl == slug_pl).first()

    def get_by_any_slug(self, db: Session, *, slug: str) -> Collection | None:
        """Look up by either ``slug`` (EN) or ``slug_pl`` (PL).

        Returns the EN match preferentially when both columns share the same
        value across rows — matches the default-locale convention.
        """
        en_match = self.get_by_slug(db, slug=slug)
        if en_match is not None:
            return en_match
        return self.get_by_slug_pl(db, slug_pl=slug)

    def get_active(self, db: Session, *, skip: int = 0, limit: int = 100) -> list[Collection]:
        return (
            db.query(Collection)
            .filter(Collection.is_active.is_(True))
            .order_by(Collection.sort_order.asc(), Collection.id.asc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def product_counts(self, db: Session) -> dict[int, int]:
        """Map ``collection_id → member product count`` for the whole catalog.

        Single query instead of N+1 — used by the list endpoints to fill in
        ``product_count`` without re-querying per row.
        """
        rows = (
            db.query(
                collection_products.c.collection_id,
                func.count(collection_products.c.product_id),
            )
            .group_by(collection_products.c.collection_id)
            .all()
        )
        return {collection_id: count for collection_id, count in rows}

    def count_products(self, db: Session, *, collection_id: int) -> int:
        """Number of products in a single collection."""
        return (
            db.query(func.count(collection_products.c.product_id))
            .filter(collection_products.c.collection_id == collection_id)
            .scalar()
            or 0
        )

    def member_product_ids(self, db: Session, *, collection_id: int) -> list[int]:
        """Member product ids in curation order (``position`` then ``product_id``)."""
        rows = db.execute(
            select(collection_products.c.product_id)
            .where(collection_products.c.collection_id == collection_id)
            .order_by(
                collection_products.c.position.asc(),
                collection_products.c.product_id.asc(),
            )
        ).all()
        return [row[0] for row in rows]

    def set_products(self, db: Session, *, collection_id: int, product_ids: list[int]) -> list[int]:
        """Replace the whole membership with ``product_ids`` (display order).

        Deletes every existing row for the collection and re-inserts one row per
        id, writing ``position`` by array index. De-duplicates while preserving
        first-seen order. Returns the set of *affected* product ids (old + new)
        so the caller can re-sync their Meili docs. Stages only — caller commits.
        """
        previous = set(self.member_product_ids(db, collection_id=collection_id))

        # De-duplicate, preserving order.
        ordered: list[int] = []
        seen: set[int] = set()
        for pid in product_ids:
            if pid not in seen:
                seen.add(pid)
                ordered.append(pid)

        db.execute(
            delete(collection_products).where(collection_products.c.collection_id == collection_id)
        )
        if ordered:
            db.execute(
                insert(collection_products),
                [
                    {"collection_id": collection_id, "product_id": pid, "position": index}
                    for index, pid in enumerate(ordered)
                ],
            )
        return sorted(previous | set(ordered))

    def add_product(self, db: Session, *, collection_id: int, product_id: int) -> bool:
        """Add a single product to the collection (appended at the end).

        Idempotent — a product already in the collection is a no-op. Returns
        ``True`` if a row was inserted. Stages only — caller commits.
        """
        already = db.execute(
            select(collection_products.c.product_id).where(
                collection_products.c.collection_id == collection_id,
                collection_products.c.product_id == product_id,
            )
        ).first()
        if already is not None:
            return False
        max_position = (
            db.query(func.max(collection_products.c.position))
            .filter(collection_products.c.collection_id == collection_id)
            .scalar()
        )
        next_position = 0 if max_position is None else max_position + 1
        db.execute(
            insert(collection_products).values(
                collection_id=collection_id,
                product_id=product_id,
                position=next_position,
            )
        )
        return True

    def remove_product(self, db: Session, *, collection_id: int, product_id: int) -> bool:
        """Remove a single product from the collection.

        Returns ``True`` if a row was deleted. Stages only — caller commits.
        """
        existing = db.execute(
            select(collection_products.c.product_id).where(
                collection_products.c.collection_id == collection_id,
                collection_products.c.product_id == product_id,
            )
        ).first()
        if existing is None:
            return False
        db.execute(
            delete(collection_products).where(
                collection_products.c.collection_id == collection_id,
                collection_products.c.product_id == product_id,
            )
        )
        return True

    def set_product_collections(
        self, db: Session, *, product_id: int, collection_ids: list[int]
    ) -> list[int]:
        """Replace a single product's collection membership with ``collection_ids``.

        The per-product mirror of ``set_products`` (which is per-collection): used
        by the CSV importer, where one row carries a product's full
        ``collection_slugs`` set. Deletes every existing membership row for the
        product, then appends it to each target collection at the end of that
        collection's order (max ``position`` + 1). De-duplicates, preserving
        first-seen order. Returns the set of *affected* collection ids (old + new)
        for caller bookkeeping. Stages only — caller commits.
        """
        previous = {
            row[0]
            for row in db.execute(
                select(collection_products.c.collection_id).where(
                    collection_products.c.product_id == product_id
                )
            ).all()
        }

        ordered: list[int] = []
        seen: set[int] = set()
        for cid in collection_ids:
            if cid not in seen:
                seen.add(cid)
                ordered.append(cid)

        db.execute(
            delete(collection_products).where(collection_products.c.product_id == product_id)
        )
        for cid in ordered:
            max_position = (
                db.query(func.max(collection_products.c.position))
                .filter(collection_products.c.collection_id == cid)
                .scalar()
            )
            next_position = 0 if max_position is None else max_position + 1
            db.execute(
                insert(collection_products).values(
                    collection_id=cid,
                    product_id=product_id,
                    position=next_position,
                )
            )
        return sorted(previous | set(ordered))


collection = CRUDCollection(Collection)
