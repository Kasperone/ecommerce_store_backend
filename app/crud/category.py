from collections.abc import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.category import Category
from app.models.product import Product
from app.schemas.category import CategoryCreate, CategoryUpdate

MAX_DEPTH = 3
"""Maximum tree depth (root → sub → sub-sub). Beyond this, ``PUT`` / ``POST`` on
``parent_id`` raises :class:`CategoryDepthExceededError`. Cheap to bump later if
the storefront ever wants deeper nesting — the cycle/ancestor walks already scale
linearly."""


class CategoryHierarchyError(Exception):
    """Base class for application-layer hierarchy violations."""


class CategoryCycleError(CategoryHierarchyError):
    """Raised when a proposed ``parent_id`` would create a cycle."""


class CategoryDepthExceededError(CategoryHierarchyError):
    """Raised when a proposed ``parent_id`` would exceed :data:`MAX_DEPTH`."""


class CategoryParentMissingError(CategoryHierarchyError):
    """Raised when ``parent_id`` points at a row that does not exist."""


class CRUDCategory(CRUDBase[Category, CategoryCreate, CategoryUpdate]):
    """CRUD operations for Category model"""

    def get_by_slug(self, db: Session, *, slug: str) -> Category | None:
        return db.query(Category).filter(Category.slug == slug).first()

    def get_active(self, db: Session, *, skip: int = 0, limit: int = 100) -> list[Category]:
        return (
            db.query(Category)
            .filter(Category.is_active.is_(True))
            .order_by(Category.sort_order.asc(), Category.id.asc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_all_ordered(self, db: Session) -> list[Category]:
        """Every row ordered by ``(parent_id NULLS FIRST, sort_order, id)``.

        Sufficient for assembling the tree in Python — caller groups by
        ``parent_id`` to build the nested structure.
        """
        return (
            db.query(Category)
            .order_by(
                Category.parent_id.asc().nullsfirst(),
                Category.sort_order.asc(),
                Category.id.asc(),
            )
            .all()
        )

    def get_ancestors(self, db: Session, *, category_id: int) -> list[Category]:
        """Walk up the parent chain from ``category_id`` (exclusive) to the root.

        Returns ancestors in order closest-first. Walks at most
        :data:`MAX_DEPTH` hops in normal operation; the loop guard at
        ``MAX_DEPTH + 2`` protects against a malformed DB that contains a
        cycle slipped in before this check existed.
        """
        ancestors: list[Category] = []
        current = db.query(Category).filter(Category.id == category_id).first()
        if current is None:
            return ancestors
        guard = MAX_DEPTH + 2
        while current.parent_id is not None and guard > 0:
            parent = db.query(Category).filter(Category.id == current.parent_id).first()
            if parent is None:
                break
            ancestors.append(parent)
            current = parent
            guard -= 1
        return ancestors

    def depth_of(self, db: Session, *, parent_id: int | None) -> int:
        """How deep a node sitting under ``parent_id`` would be.

        Root (``parent_id is None``) → depth 1. A node whose parent is a root
        → depth 2. The route layer rejects any proposed parent that would
        push a node past :data:`MAX_DEPTH`.
        """
        if parent_id is None:
            return 1
        # Walk up from the proposed parent, counting hops. ``get_ancestors``
        # returns ancestors of parent_id (exclusive of parent_id itself), so
        # length + 1 (for parent itself) + 1 (for the new node) is depth.
        ancestors = self.get_ancestors(db, category_id=parent_id)
        return len(ancestors) + 2

    def subtree_height(self, db: Session, *, root_id: int) -> int:
        """Number of edges from ``root_id`` down to its deepest descendant.

        Returns 0 for a leaf. Used by the PUT route to reject reparenting a
        subtree that would push its leaves past :data:`MAX_DEPTH`.
        """
        height = 0
        frontier = [root_id]
        guard = MAX_DEPTH + 2
        while guard > 0:
            child_ids = [
                cid
                for (cid,) in db.query(Category.id).filter(Category.parent_id.in_(frontier)).all()
            ]
            if not child_ids:
                break
            height += 1
            frontier = child_ids
            guard -= 1
        return height

    def is_descendant(self, db: Session, *, candidate_id: int, of_id: int) -> bool:
        """Is ``candidate_id`` a descendant of ``of_id``? (Or the same node.)

        Used during ``PUT /{id}`` to reject ``parent_id = self`` or
        ``parent_id = some descendant of self``, which would create a cycle.
        """
        if candidate_id == of_id:
            return True
        ancestors = self.get_ancestors(db, category_id=candidate_id)
        return any(a.id == of_id for a in ancestors)

    def count_subtree_categories(self, db: Session, *, root_id: int) -> int:
        """Number of categories strictly below ``root_id`` in the tree."""
        return len(self._subtree_ids(db, root_id=root_id)) - 1

    def count_subtree_products(self, db: Session, *, root_id: int) -> int:
        """Sum of active products across ``root_id`` and all descendants."""
        ids = self._subtree_ids(db, root_id=root_id)
        return (
            db.query(func.count(Product.id))
            .filter(Product.category_id.in_(ids))
            .filter(Product.is_active.is_(True))
            .scalar()
            or 0
        )

    def count_direct_products(self, db: Session, *, category_id: int) -> int:
        """Products attached directly to ``category_id`` (any active state)."""
        return (
            db.query(func.count(Product.id)).filter(Product.category_id == category_id).scalar()
            or 0
        )

    def _subtree_ids(self, db: Session, *, root_id: int) -> list[int]:
        """BFS descent over the tree, returning [root_id, *descendants]."""
        result: list[int] = [root_id]
        frontier: list[int] = [root_id]
        guard = MAX_DEPTH + 2
        while frontier and guard > 0:
            child_ids = [
                cid
                for (cid,) in db.query(Category.id).filter(Category.parent_id.in_(frontier)).all()
            ]
            if not child_ids:
                break
            result.extend(child_ids)
            frontier = child_ids
            guard -= 1
        return result

    def delete_subtree(self, db: Session, *, root: Category) -> list[Category]:
        """Cascade-delete ``root`` and every descendant.

        Returns the list of deleted rows in *deepest-first* order so the
        caller can record one audit event per row before the rows leave the
        session. The caller commits.
        """
        ordered = self._collect_subtree_rows(db, root=root)
        # Deepest-first so FK ON DELETE RESTRICT does not trip on parents.
        for cat in reversed(ordered):
            db.delete(cat)
        return list(reversed(ordered))

    def _collect_subtree_rows(self, db: Session, *, root: Category) -> list[Category]:
        ids = self._subtree_ids(db, root_id=root.id)
        return db.query(Category).filter(Category.id.in_(ids)).all()

    def reparent_children_to_root(self, db: Session, *, parent_id: int) -> int:
        """Set ``parent_id = NULL`` on every direct child of ``parent_id``.

        Returns the number of rows mutated. The caller commits and is
        responsible for the audit trail on each touched row.
        """
        children = db.query(Category).filter(Category.parent_id == parent_id).all()
        for child in children:
            child.parent_id = None
        return len(children)

    def bulk_reorder(
        self,
        db: Session,
        *,
        items: Iterable[tuple[int, int | None, int]],
    ) -> list[Category]:
        """Apply ``(id, parent_id, sort_order)`` tuples in a single pass.

        Validates depth + cycle constraints across the whole proposed
        layout BEFORE mutating any row, so a single bad item rolls the
        full batch back. Returns the list of rows actually touched.

        Validation strategy:

        1. Load every affected row in one query.
        2. Build a proposed parent_id map (id → proposed parent_id).
        3. For each row, walk up via the *proposed* map; reject on cycle
           (id appears in its own ancestor chain) or depth > MAX_DEPTH.
        4. Apply mutations.
        """
        items_list = list(items)
        if not items_list:
            return []

        ids = [item[0] for item in items_list]
        rows = db.query(Category).filter(Category.id.in_(ids)).all()
        rows_by_id = {row.id: row for row in rows}
        missing = [item_id for item_id in ids if item_id not in rows_by_id]
        if missing:
            raise CategoryParentMissingError(
                f"bulk_reorder references missing categories: {missing}"
            )

        # Snapshot the current parent layout for every row in the DB, then
        # overlay the proposed changes — so a cycle check considers nodes
        # we are NOT touching this batch too.
        all_rows = db.query(Category).all()
        proposed_parent: dict[int, int | None] = {r.id: r.parent_id for r in all_rows}
        for item_id, new_parent, _ in items_list:
            proposed_parent[item_id] = new_parent

        # Verify proposed parents exist
        for item_id, new_parent, _ in items_list:
            if new_parent is not None and new_parent not in proposed_parent:
                raise CategoryParentMissingError(
                    f"proposed parent_id={new_parent} for id={item_id} does not exist"
                )

        # Walk every row in the proposed layout — not just the touched
        # ones — so descendants of a moved subtree get depth-checked too.
        for row_id in proposed_parent:
            self._validate_proposed_chain(row_id, proposed_parent)

        for item_id, new_parent, new_sort in items_list:
            row = rows_by_id[item_id]
            row.parent_id = new_parent
            row.sort_order = new_sort

        return list(rows_by_id.values())

    @staticmethod
    def _validate_proposed_chain(
        start_id: int,
        proposed_parent: dict[int, int | None],
    ) -> None:
        """Walk up ``proposed_parent`` from ``start_id``; raise on cycle/depth."""
        seen: set[int] = set()
        current: int | None = start_id
        depth = 1
        while current is not None:
            if current in seen:
                raise CategoryCycleError(f"bulk_reorder would create a cycle through id={start_id}")
            seen.add(current)
            if depth > MAX_DEPTH:
                raise CategoryDepthExceededError(
                    f"Category depth limit ({MAX_DEPTH}) exceeded for id={start_id}"
                )
            current = proposed_parent.get(current)
            depth += 1


category = CRUDCategory(Category)
