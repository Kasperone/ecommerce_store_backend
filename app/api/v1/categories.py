import logging
import uuid
from collections import defaultdict
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.capabilities import CATALOG_WRITE
from app.core.config import settings
from app.core.dependencies import get_current_admin, get_db, require_capability
from app.core.storage import get_r2_client
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
    CategoryHeroAltUpdate,
    CategoryHeroResponse,
    CategoryResponse,
    CategoryTreeNode,
    CategoryUpdate,
    CategoryWithProductCount,
)
from app.services import search_index
from app.services.audit import AuditWriter, get_audit_writer
from app.services.image import (
    MIME_TO_EXTENSION,
    capture_dimensions,
    delete_from_r2,
    read_with_size_cap,
    upload_to_r2,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Capability gate for this router's write/DELETE routes.
_require = Depends(require_capability(CATALOG_WRITE))

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


def _check_slug_pl_collision(
    db: Session,
    *,
    slug_pl: str | None,
    exclude_id: int | None,
) -> None:
    """Reject a slug_pl that another row already owns.

    The DB has a partial unique index on ``slug_pl WHERE NOT NULL`` so a
    collision would also surface as an IntegrityError on commit — but routing
    that through Pydantic gives a clean 400 instead of a 500. ``exclude_id``
    is the row being edited (skipped during the lookup so a no-op PUT
    doesn't fight itself).
    """
    if not slug_pl:
        return
    existing = crud_category.get_by_slug_pl(db, slug_pl=slug_pl)
    if existing is not None and existing.id != exclude_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category with this slug_pl already exists",
        )


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
    """Get category by slug with product count.

    Searches both ``slug`` (EN) and ``slug_pl`` (PL). When the same value
    exists across both columns on different rows, the EN row wins —
    matches the default-locale convention so an English request that
    accidentally collides with a Polish slug still resolves predictably.
    """
    category = crud_category.get_by_any_slug(db, slug=slug)
    if not category:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

    product_count = crud_category.count_direct_products(db, category_id=category.id)

    cat_dict = CategoryResponse.model_validate(category).model_dump()
    cat_dict["product_count"] = product_count
    return cat_dict


@router.post(
    "/",
    dependencies=[_require],
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
)
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

    _check_slug_pl_collision(db, slug_pl=category_in.slug_pl, exclude_id=None)
    _validate_parent(db, proposed_parent_id=category_in.parent_id, self_id=None)

    category = crud_category.create(db, obj_in=category_in)
    audit.record_create(resource_type="category", obj=category)
    db.commit()
    db.refresh(category)
    # Best-effort: index the new category (no-op when Meili unconfigured).
    search_index.sync_category(db, category.id)
    return category


@router.post("/bulk-reorder", dependencies=[_require], response_model=list[CategoryTreeNode])
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


@router.put("/{category_id}", dependencies=[_require], response_model=CategoryResponse)
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

    if "slug_pl" in category_in.model_fields_set and category_in.slug_pl != category.slug_pl:
        _check_slug_pl_collision(db, slug_pl=category_in.slug_pl, exclude_id=category.id)

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
    # Re-index the category, and its products too — the name is denormalized
    # onto each product doc as category_name/category_name_pl (best-effort).
    search_index.sync_category(db, category.id)
    search_index.sync_products_for_category(db, category.id)
    return category


@router.delete("/{category_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT)
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
        search_index.delete_category(category_id)
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
        deleted_ids = [row.id for row in deleted_rows]
        for row in deleted_rows:
            audit.record_delete(resource_type="category", obj=row)
        db.commit()
        for deleted_id in deleted_ids:
            search_index.delete_category(deleted_id)
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
    child_ids = [child.id for child in children]
    for child in children:
        child.parent_id = None
        audit.record_update(resource_type="category", obj=child)
    audit.record_delete(resource_type="category", obj=category)
    crud_category.delete(db, id=category_id)
    db.commit()
    search_index.delete_category(category_id)
    # Re-index lifted children — their parent_name is now null.
    for child_id in child_ids:
        search_index.sync_category(db, child_id)
    return None


# ---------------------------------------------------------------------------
# Hero image
# ---------------------------------------------------------------------------


def _r2_key_from_hero_url(url: str) -> str:
    """Derive the R2 object key from the public hero URL.

    Mirrors the products upload pattern (``app/api/v1/uploads.py``). Stored
    keys live under ``categories/{id}/`` so the bucket layout is obvious.
    """
    return url.removeprefix(settings.R2_PUBLIC_URL).lstrip("/")


@router.post(
    "/{category_id}/hero",
    dependencies=[_require],
    response_model=CategoryHeroResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_category_hero(
    category_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Upload (or replace) the hero image for ``category_id`` (Admin only).

    Reuses the products upload pipeline (magic-byte sniff → size cap →
    Pillow dimensions → R2 put) so the security posture stays consistent
    across the catalog. ``get_r2_client()`` is called inline AFTER
    validation so a spoofed extension surfaces as 400 even on dev
    environments where R2 is unconfigured.

    If a previous hero exists, the new upload overwrites the columns; the
    old R2 object is best-effort deleted (a failure logs and proceeds —
    the row already reflects the new key, an orphan object is a janitor's
    problem, not a request-path one).
    """
    category = crud_category.get(db, id=category_id)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")

    body, detected_mime = await read_with_size_cap(file)
    width, height = capture_dimensions(body)

    r2 = get_r2_client()

    ext = MIME_TO_EXTENSION[detected_mime]
    key = f"categories/{category_id}/{uuid.uuid4().hex}.{ext}"
    public_url = upload_to_r2(
        r2,
        bucket=settings.R2_BUCKET_NAME,
        key=key,
        body=body,
        content_type=detected_mime,
    )

    previous_url = category.hero_image_url
    category.hero_image_url = public_url
    category.hero_width = width
    category.hero_height = height
    category.hero_bytes = len(body)
    category.hero_mime = detected_mime
    # alt_text intentionally preserved on re-upload; admins use PATCH to edit it.

    audit.record_update(resource_type="category", obj=category)
    db.commit()
    db.refresh(category)

    if previous_url:
        previous_key = _r2_key_from_hero_url(previous_url)
        if previous_key and not delete_from_r2(
            r2, bucket=settings.R2_BUCKET_NAME, key=previous_key
        ):
            logger.warning(
                "R2 delete of previous hero failed for category_id=%s key=%s",
                category_id,
                previous_key,
            )

    return CategoryHeroResponse.model_validate(category)


@router.patch("/{category_id}/hero", dependencies=[_require], response_model=CategoryHeroResponse)
async def update_category_hero_alt(
    category_id: int,
    body: CategoryHeroAltUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update only the alt text on the hero image (Admin only).

    Re-upload uses ``POST /hero``. This endpoint exists so an admin can
    type a description without re-sending the binary.
    """
    category = crud_category.get(db, id=category_id)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    if category.hero_image_url is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category has no hero image. Upload one before editing alt text.",
        )

    category.hero_image_alt = body.hero_image_alt
    audit.record_update(resource_type="category", obj=category)
    db.commit()
    db.refresh(category)
    return CategoryHeroResponse.model_validate(category)


@router.delete(
    "/{category_id}/hero", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_category_hero(
    category_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Clear the hero columns and best-effort delete the R2 object."""
    category = crud_category.get(db, id=category_id)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found")
    if category.hero_image_url is None:
        return None  # idempotent

    r2 = get_r2_client()
    key = _r2_key_from_hero_url(category.hero_image_url)
    if key and not delete_from_r2(r2, bucket=settings.R2_BUCKET_NAME, key=key):
        logger.warning(
            "R2 delete failed for category_id=%s key=%s; columns cleared anyway",
            category_id,
            key,
        )

    category.hero_image_url = None
    category.hero_image_alt = None
    category.hero_width = None
    category.hero_height = None
    category.hero_bytes = None
    category.hero_mime = None
    audit.record_update(resource_type="category", obj=category)
    db.commit()
    return None
