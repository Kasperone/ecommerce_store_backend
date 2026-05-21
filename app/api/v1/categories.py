from collections import defaultdict
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_admin, get_db
from app.crud import category as crud_category
from app.crud.category import (
    MAX_DEPTH,
    CategoryCycleError,
    CategoryDepthExceededError,
    CategoryHierarchyError,
    CategoryParentMissingError,
)
from app.models.category import Category
from app.models.product import Product
from app.models.user import User
from app.schemas.category import (
    BulkReorderRequest,
    CategoryCreate,
    CategoryDeletePreview,
    CategoryResponse,
    CategoryTreeNode,
    CategoryUpdate,
    CategoryWithProductCount,
)
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

DeleteMode = Literal["cascade", "reparent_to_root"]


def _active_product_counts(db: Session) -> dict[int, int]:
    """Map ``category_id → active product count`` for the whole catalog.

    Single query instead of N+1 — used by ``GET /`` and ``GET /tree`` to
    fill in ``product_count`` without re-querying per row.
    """
    rows = (
        db.query(Product.category_id, func.count(Product.id))
        .filter(Product.is_active.is_(True))
        .group_by(Product.category_id)
        .all()
    )
    return {cat_id: count for cat_id, count in rows if cat_id is not None}


def _validate_parent(
    db: Session,
    *,
    proposed_parent_id: int | None,
    self_id: int | None,
) -> None:
    """Shared depth + cycle + existence check for POST/PUT parent_id.

    ``self_id`` is ``None`` on POST (no row exists yet) and the row's id on
    PUT. Raises ``HTTPException`` directly so call-sites stay flat.
    """
    if proposed_parent_id is None:
        return
    parent = crud_category.get(db, id=proposed_parent_id)
    if parent is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"parent_id={proposed_parent_id} does not exist",
        )
    if self_id is not None and crud_category.is_descendant(
        db, candidate_id=proposed_parent_id, of_id=self_id
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="parent_id would create a cycle",
        )
    new_depth = crud_category.depth_of(db, parent_id=proposed_parent_id)
    descendant_height = (
        crud_category.subtree_height(db, root_id=self_id) if self_id is not None else 0
    )
    if new_depth + descendant_height > MAX_DEPTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Category depth limit ({MAX_DEPTH}) exceeded",
        )


@router.get("/tree", response_model=list[CategoryTreeNode])
async def get_category_tree(
    db: Session = Depends(get_db),
    is_active: bool | None = Query(
        None,
        description="Filter to active categories only; omit for the full tree",
    ),
):
    """Return the full category tree.

    Sibling order is ``(sort_order, id)`` (set in :func:`get_all_ordered`).
    Each node carries ``product_count`` (active products attached directly to
    that node) — descendants are NOT summed in.
    """
    all_rows = crud_category.get_all_ordered(db)
    if is_active is not None:
        all_rows = [r for r in all_rows if r.is_active is is_active]
    counts = _active_product_counts(db)

    by_parent: dict[int | None, list[Category]] = defaultdict(list)
    for row in all_rows:
        by_parent[row.parent_id].append(row)

    def build(row: Category) -> CategoryTreeNode:
        node = CategoryTreeNode.model_validate(row).model_copy(
            update={
                "product_count": counts.get(row.id, 0),
                "children": [build(child) for child in by_parent.get(row.id, [])],
            }
        )
        return node

    return [build(root) for root in by_parent.get(None, [])]


@router.get("/", response_model=list[CategoryWithProductCount])
async def list_categories(
    db: Session = Depends(get_db),
    skip: int = Query(0, ge=0, description="Offset for pagination"),
    limit: int = Query(100, ge=1, le=100, description="Limit for pagination"),
    is_active: bool = Query(True, description="Filter by active status"),
):
    """
    Get list of categories with product counts

    - **skip**: Offset for pagination
    - **limit**: Number of items (1-100)
    - **is_active**: Show only active categories (default: true)
    """
    if is_active:
        categories = crud_category.get_active(db, skip=skip, limit=limit)
    else:
        categories = crud_category.get_multi(db, skip=skip, limit=limit)

    counts = _active_product_counts(db)
    result = []
    for cat in categories:
        cat_dict = CategoryResponse.model_validate(cat).model_dump()
        cat_dict["product_count"] = counts.get(cat.id, 0)
        result.append(cat_dict)
    return result


@router.get("/{category_id}", response_model=CategoryWithProductCount)
async def get_category(
    category_id: int,
    db: Session = Depends(get_db),
):
    """
    Get category by ID with product count
    """
    category = crud_category.get(db, id=category_id)
    if not category:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

    product_count = crud_category.count_direct_products(db, category_id=category.id)

    cat_dict = CategoryResponse.model_validate(category).model_dump()
    cat_dict["product_count"] = product_count
    return cat_dict


@router.get("/{category_id}/delete-preview", response_model=CategoryDeletePreview)
async def category_delete_preview(
    category_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Counts the admin needs before picking ``cascade`` vs ``reparent_to_root``."""
    category = crud_category.get(db, id=category_id)
    if not category:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    return CategoryDeletePreview(
        descendant_count=crud_category.count_subtree_categories(db, root_id=category_id),
        direct_product_count=crud_category.count_direct_products(db, category_id=category_id),
        subtree_product_count=crud_category.count_subtree_products(db, root_id=category_id),
    )


@router.get("/slug/{slug}", response_model=CategoryWithProductCount)
async def get_category_by_slug(
    slug: str,
    db: Session = Depends(get_db),
):
    """
    Get category by slug with product count
    """
    category = crud_category.get_by_slug(db, slug=slug)
    if not category:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

    product_count = crud_category.count_direct_products(db, category_id=category.id)

    cat_dict = CategoryResponse.model_validate(category).model_dump()
    cat_dict["product_count"] = product_count
    return cat_dict


@router.post("/", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category(
    category_in: CategoryCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """
    Create new category (Admin only)

    Requires admin authentication
    """
    existing = crud_category.get_by_slug(db, slug=category_in.slug)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Category with this slug already exists"
        )

    _validate_parent(db, proposed_parent_id=category_in.parent_id, self_id=None)

    category = crud_category.create(db, obj_in=category_in)
    audit.record_create(resource_type="category", obj=category)
    db.commit()
    db.refresh(category)
    return category


@router.post("/bulk-reorder", response_model=list[CategoryTreeNode])
async def bulk_reorder_categories(
    body: BulkReorderRequest,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Apply a whole-tree reorder in a single transaction.

    The server validates depth + cycle constraints over the *full proposed*
    layout (existing rows the admin did not touch are included in the
    walk), so a partial-tree request still cannot create a cycle through
    rows the admin omitted. Returns the freshly-computed tree.
    """
    items = [(item.id, item.parent_id, item.sort_order) for item in body.items]
    try:
        touched = crud_category.bulk_reorder(db, items=items)
    except CategoryParentMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except CategoryDepthExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except CategoryCycleError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    for row in touched:
        audit.record_update(resource_type="category", obj=row)
    db.commit()

    # Re-assemble the freshly-committed tree (cheap; ≤ 3 levels).
    return await get_category_tree(db=db, is_active=None)


@router.put("/{category_id}", response_model=CategoryResponse)
async def update_category(
    category_id: int,
    category_in: CategoryUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """
    Update category (Admin only)

    Requires admin authentication
    """
    category = crud_category.get(db, id=category_id)
    if not category:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

    if category_in.slug and category_in.slug != category.slug:
        existing = crud_category.get_by_slug(db, slug=category_in.slug)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Category with this slug already exists",
            )

    if "parent_id" in category_in.model_fields_set:
        _validate_parent(
            db,
            proposed_parent_id=category_in.parent_id,
            self_id=category.id,
        )

    category = crud_category.update(db, db_obj=category, obj_in=category_in)
    audit.record_update(resource_type="category", obj=category)
    db.commit()
    db.refresh(category)
    return category


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
    mode: DeleteMode | None = Query(
        None,
        description="`cascade` deletes the subtree; `reparent_to_root` lifts direct "
        "children to the root before deleting. Omit to require the category be empty.",
    ),
):
    """
    Delete category (Admin only)

    Requires admin authentication. With no ``mode`` query param, a category
    with children or products is rejected with a 400. ``mode=cascade``
    recursively deletes; ``mode=reparent_to_root`` re-parents direct
    children to the root first. Products on the deleted row still block —
    products are never silently re-parented.
    """
    category = crud_category.get(db, id=category_id)
    if not category:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

    direct_product_count = crud_category.count_direct_products(db, category_id=category_id)
    descendant_count = crud_category.count_subtree_categories(db, root_id=category_id)

    if mode is None:
        if descendant_count > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot delete category with {descendant_count} child categories. "
                    "Pass ?mode=cascade or ?mode=reparent_to_root."
                ),
            )
        if direct_product_count > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot delete category with {direct_product_count} products. "
                    "Remove products first."
                ),
            )
        audit.record_delete(resource_type="category", obj=category)
        crud_category.delete(db, id=category_id)
        db.commit()
        return None

    if mode == "cascade":
        if crud_category.count_subtree_products(db, root_id=category_id) > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Cascade delete would leave products orphaned. Remove products from "
                    "the subtree first."
                ),
            )
        # Snapshot all rows before delete — the audit writer reads them.
        try:
            deleted_rows = crud_category.delete_subtree(db, root=category)
        except CategoryHierarchyError as exc:  # pragma: no cover - defensive
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(exc),
            ) from exc
        for row in deleted_rows:
            audit.record_delete(resource_type="category", obj=row)
        db.commit()
        return None

    # mode == "reparent_to_root"
    if direct_product_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot delete category with {direct_product_count} products. "
                "Remove products first."
            ),
        )
    children = db.query(Category).filter(Category.parent_id == category_id).all()
    for child in children:
        child.parent_id = None
        audit.record_update(resource_type="category", obj=child)
    audit.record_delete(resource_type="category", obj=category)
    crud_category.delete(db, id=category_id)
    db.commit()
    return None
