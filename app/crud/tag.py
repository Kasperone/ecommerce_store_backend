"""CRUD for the customer-tag vocabulary + the customer↔tag join.

Mutating methods stage on the session and (for creates) flush so ``id`` is
readable, but never commit — the route owns the transaction boundary.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.tag import Tag, customer_tags
from app.models.user import User
from app.schemas.tag import TagCreate, TagUpdate


def normalize_name(name: str) -> str:
    """Trim and collapse internal whitespace (the canonical stored form)."""
    return " ".join(name.split())


class CRUDTag(CRUDBase[Tag, TagCreate, TagUpdate]):
    def list_with_counts(self, db: Session) -> list[tuple[Tag, int]]:
        """All tags with their customer usage_count, ordered by name."""
        rows = (
            db.query(Tag, func.count(customer_tags.c.user_id))
            .outerjoin(customer_tags, customer_tags.c.tag_id == Tag.id)
            .group_by(Tag.id)
            .order_by(func.lower(Tag.name))
            .all()
        )
        return [(t, count) for t, count in rows]

    def usage_count(self, db: Session, *, tag_id: int) -> int:
        """How many customers carry this tag."""
        return (
            db.query(func.count(customer_tags.c.user_id))
            .filter(customer_tags.c.tag_id == tag_id)
            .scalar()
            or 0
        )

    def get_by_name_ci(self, db: Session, *, name: str) -> Tag | None:
        """Case-insensitive lookup by (normalized) name."""
        return db.query(Tag).filter(func.lower(Tag.name) == normalize_name(name).lower()).first()

    def create_tag(self, db: Session, *, name: str, color: str | None = None) -> Tag:
        """Stage a new tag (normalized name). Flushes for the id; caller commits."""
        tag = Tag(name=normalize_name(name), color=color)
        db.add(tag)
        db.flush()
        db.refresh(tag)
        return tag

    def get_or_create(self, db: Session, *, name: str, color: str | None = None) -> Tag:
        """Return the existing tag (ci match) or stage a new one."""
        existing = self.get_by_name_ci(db, name=name)
        if existing is not None:
            return existing
        return self.create_tag(db, name=name, color=color)

    def attach(self, db: Session, *, user: User, tag: Tag) -> bool:
        """Link a tag to a customer. Idempotent — returns True only if newly added."""
        if tag in user.tags:
            return False
        user.tags.append(tag)
        return True

    def detach(self, db: Session, *, user: User, tag: Tag) -> bool:
        """Unlink a tag from a customer. Returns True only if it was present."""
        if tag not in user.tags:
            return False
        user.tags.remove(tag)
        return True


tag = CRUDTag(Tag)
