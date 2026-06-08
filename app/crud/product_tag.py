"""CRUD for the product-tag vocabulary + the product↔tag join.

Mutating methods stage on the session and (for creates) flush so ``id`` is
readable, but never commit — the route owns the transaction boundary.

Mirrors ``app/crud/tag.py`` (customer tags) with strictly distinct ``ProductTag``
names so the two systems never collide.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.product_tag import ProductTag, product_tag_links
from app.schemas.product_tag import ProductTagCreate, ProductTagUpdate


def normalize_name(name: str) -> str:
    """Trim and collapse internal whitespace (the canonical stored form)."""
    return " ".join(name.split())


class CRUDProductTag(CRUDBase[ProductTag, ProductTagCreate, ProductTagUpdate]):
    def list_with_counts(self, db: Session) -> list[tuple[ProductTag, int]]:
        """All tags with their product usage_count, ordered by name."""
        rows = (
            db.query(ProductTag, func.count(product_tag_links.c.product_id))
            .outerjoin(product_tag_links, product_tag_links.c.tag_id == ProductTag.id)
            .group_by(ProductTag.id)
            .order_by(func.lower(ProductTag.name))
            .all()
        )
        return [(t, count) for t, count in rows]

    def usage_count(self, db: Session, *, tag_id: int) -> int:
        """How many products carry this tag."""
        return (
            db.query(func.count(product_tag_links.c.product_id))
            .filter(product_tag_links.c.tag_id == tag_id)
            .scalar()
            or 0
        )

    def get_by_name_ci(self, db: Session, *, name: str) -> ProductTag | None:
        """Case-insensitive lookup by (normalized) name."""
        return (
            db.query(ProductTag)
            .filter(func.lower(ProductTag.name) == normalize_name(name).lower())
            .first()
        )

    def create_tag(self, db: Session, *, name: str, color: str | None = None) -> ProductTag:
        """Stage a new tag (normalized name). Flushes for the id; caller commits."""
        tag = ProductTag(name=normalize_name(name), color=color)
        db.add(tag)
        db.flush()
        db.refresh(tag)
        return tag

    def get_or_create(self, db: Session, *, name: str, color: str | None = None) -> ProductTag:
        """Return the existing tag (ci match) or stage a new one."""
        existing = self.get_by_name_ci(db, name=name)
        if existing is not None:
            return existing
        return self.create_tag(db, name=name, color=color)


product_tag = CRUDProductTag(ProductTag)
