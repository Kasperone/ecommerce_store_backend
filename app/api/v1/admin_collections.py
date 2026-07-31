"""Admin collection API (/api/v1/admin/collections).

CRUD + hero image + membership management over manual collections. Mirrors
``categories.py`` (slug/slug_pl collision → 400, the three hero endpoints reusing
the ``app/services/image.py`` pipeline under a ``collections/{id}/`` R2 key
layout). Admin-gated; every mutation is audited (``resource_type="collection"``
for the row, ``resource_type="collection_product"`` for membership).

After any membership change — or a rename that flips a member product's
``collection_slugs`` — the affected products' Meili docs are re-synced
(``search_index.sync_products_for_collection`` / ``sync_products_bulk``).
"""

import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.capabilities import CATALOG_WRITE
from app.core.config import settings
from app.core.dependencies import get_current_admin, get_db, require_capability
from app.core.storage import get_r2_client
from app.crud import collection as crud_collection
from app.crud import product as crud_product
from app.models.user import User
from app.schemas.collection import (
    CollectionCreate,
    CollectionHeroAltUpdate,
    CollectionHeroResponse,
    CollectionProductsSet,
    CollectionResponse,
    CollectionUpdate,
    CollectionWithProductCount,
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
from app.services.translation_admin import purge_translation_meta

logger = logging.getLogger(__name__)

router = APIRouter()

# Capability gate for this router's write/DELETE routes.
_require = Depends(require_capability(CATALOG_WRITE))


def _check_slug_pl_collision(
    db: Session,
    *,
    slug_pl: str | None,
    exclude_id: int | None,
) -> None:
    """Reject a slug_pl another collection already owns (clean 400, not 500).

    The DB has a partial unique index on ``slug_pl WHERE NOT NULL`` so a clash
    would also surface as an IntegrityError on commit — routing it through here
    gives a clean 400. ``exclude_id`` is the row being edited (skipped so a no-op
    PUT doesn't fight itself).
    """
    if not slug_pl:
        return
    existing = crud_collection.get_by_slug_pl(db, slug_pl=slug_pl)
    if existing is not None and existing.id != exclude_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Collection with this slug_pl already exists",
        )


def _with_count(db: Session, collection) -> dict:
    col_dict = CollectionResponse.model_validate(collection).model_dump()
    col_dict["product_count"] = crud_collection.count_products(db, collection_id=collection.id)
    return col_dict


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("/", response_model=list[CollectionWithProductCount])
async def list_collections(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """List every collection (active or not) with member product counts (admin)."""
    collections = crud_collection.get_multi(db, skip=0, limit=100)
    counts = crud_collection.product_counts(db)
    result = []
    for col in collections:
        col_dict = CollectionResponse.model_validate(col).model_dump()
        col_dict["product_count"] = counts.get(col.id, 0)
        result.append(col_dict)
    return result


@router.get("/{collection_id}", response_model=CollectionWithProductCount)
async def get_collection(
    collection_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Get a collection by id with its product count (admin)."""
    collection = crud_collection.get(db, id=collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    return _with_count(db, collection)


@router.post(
    "/",
    dependencies=[_require],
    response_model=CollectionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_collection(
    collection_in: CollectionCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a collection (admin). 400 on slug / slug_pl collision."""
    existing = crud_collection.get_by_slug(db, slug=collection_in.slug)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Collection with this slug already exists",
        )
    _check_slug_pl_collision(db, slug_pl=collection_in.slug_pl, exclude_id=None)

    collection = crud_collection.create(db, obj_in=collection_in)
    audit.record_create(resource_type="collection", obj=collection)
    db.commit()
    db.refresh(collection)
    return collection


@router.put("/{collection_id}", dependencies=[_require], response_model=CollectionResponse)
async def update_collection(
    collection_id: int,
    collection_in: CollectionUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update a collection (admin). 400 on slug / slug_pl collision.

    A rename re-syncs member products' Meili docs — their ``collection_slugs``
    labels may have changed.
    """
    collection = crud_collection.get(db, id=collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")

    if collection_in.slug and collection_in.slug != collection.slug:
        existing = crud_collection.get_by_slug(db, slug=collection_in.slug)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Collection with this slug already exists",
            )

    if "slug_pl" in collection_in.model_fields_set and collection_in.slug_pl != collection.slug_pl:
        _check_slug_pl_collision(db, slug_pl=collection_in.slug_pl, exclude_id=collection.id)

    collection = crud_collection.update(db, db_obj=collection, obj_in=collection_in)
    audit.record_update(resource_type="collection", obj=collection)
    db.commit()
    db.refresh(collection)
    # A slug change flips the member products' collection_slugs (best-effort).
    search_index.sync_products_for_collection(db, collection.id)
    return collection


@router.delete("/{collection_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(
    collection_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a collection (admin). The membership join cascades.

    Member products' docs are re-synced afterwards (they lost a collection_slug).
    """
    collection = crud_collection.get(db, id=collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")

    affected = crud_collection.member_product_ids(db, collection_id=collection_id)
    audit.record_delete(resource_type="collection", obj=collection)
    crud_collection.delete(db, id=collection_id)
    # The translation sidecar is polymorphic, so it carries no FK and nothing
    # at the database level cleans up after it.
    purge_translation_meta(db, resource_type="collection", resource_ids=[collection_id])
    db.commit()
    search_index.sync_products_bulk(db, affected)
    return None


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


@router.put(
    "/{collection_id}/products", dependencies=[_require], response_model=CollectionWithProductCount
)
async def set_collection_products(
    collection_id: int,
    body: CollectionProductsSet,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Replace a collection's membership in display order (admin).

    ``product_ids`` is the full ordered membership; the join is replaced in one
    transaction, ``position`` written by array index. Unknown product ids are
    rejected with a 422. Affected products (old + new) get their Meili docs
    re-synced.
    """
    collection = crud_collection.get(db, id=collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")

    for pid in set(body.product_ids):
        if crud_product.get(db, id=pid) is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"product_id={pid} does not exist",
            )

    affected = crud_collection.set_products(
        db, collection_id=collection_id, product_ids=body.product_ids
    )
    audit.record_event(
        action="update",
        resource_type="collection_product",
        resource_id=collection_id,
        payload={"product_ids": body.product_ids},
    )
    db.commit()
    search_index.sync_products_bulk(db, affected)

    db.refresh(collection)
    return _with_count(db, collection)


@router.post(
    "/{collection_id}/products/{product_id}",
    dependencies=[_require],
    response_model=CollectionWithProductCount,
    status_code=status.HTTP_201_CREATED,
)
async def add_collection_product(
    collection_id: int,
    product_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Add a single product to a collection (admin, idempotent, appended)."""
    collection = crud_collection.get(db, id=collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if crud_product.get(db, id=product_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    inserted = crud_collection.add_product(db, collection_id=collection_id, product_id=product_id)
    if inserted:
        audit.record_event(
            action="create",
            resource_type="collection_product",
            resource_id=collection_id,
            payload={"product_id": product_id},
        )
    db.commit()
    if inserted:
        search_index.sync_product(db, product_id)

    db.refresh(collection)
    return _with_count(db, collection)


@router.delete(
    "/{collection_id}/products/{product_id}",
    dependencies=[_require],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_collection_product(
    collection_id: int,
    product_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Remove a single product from a collection (admin)."""
    collection = crud_collection.get(db, id=collection_id)
    if not collection:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")

    removed = crud_collection.remove_product(db, collection_id=collection_id, product_id=product_id)
    if removed:
        audit.record_event(
            action="delete",
            resource_type="collection_product",
            resource_id=collection_id,
            payload={"product_id": product_id},
        )
    db.commit()
    if removed:
        search_index.sync_product(db, product_id)
    return None


# ---------------------------------------------------------------------------
# Hero image
# ---------------------------------------------------------------------------


def _r2_key_from_hero_url(url: str) -> str:
    """Derive the R2 object key from the public hero URL (mirrors categories)."""
    return url.removeprefix(settings.R2_PUBLIC_URL).lstrip("/")


@router.post(
    "/{collection_id}/hero",
    dependencies=[_require],
    response_model=CollectionHeroResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_collection_hero(
    collection_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Upload (or replace) the hero image for ``collection_id`` (admin).

    Reuses the products upload pipeline (magic-byte sniff → size cap → Pillow
    dimensions → R2 put) under a ``collections/{id}/`` key layout. The previous
    R2 object is best-effort deleted.
    """
    collection = crud_collection.get(db, id=collection_id)
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")

    body, detected_mime = await read_with_size_cap(file)
    width, height = capture_dimensions(body)

    r2 = get_r2_client()

    ext = MIME_TO_EXTENSION[detected_mime]
    key = f"collections/{collection_id}/{uuid.uuid4().hex}.{ext}"
    public_url = upload_to_r2(
        r2,
        bucket=settings.R2_BUCKET_NAME,
        key=key,
        body=body,
        content_type=detected_mime,
    )

    previous_url = collection.hero_image_url
    collection.hero_image_url = public_url
    collection.hero_width = width
    collection.hero_height = height
    collection.hero_bytes = len(body)
    collection.hero_mime = detected_mime
    # alt_text intentionally preserved on re-upload; admins use PATCH to edit it.

    audit.record_update(resource_type="collection", obj=collection)
    db.commit()
    db.refresh(collection)

    if previous_url:
        previous_key = _r2_key_from_hero_url(previous_url)
        if previous_key and not delete_from_r2(
            r2, bucket=settings.R2_BUCKET_NAME, key=previous_key
        ):
            logger.warning(
                "R2 delete of previous hero failed for collection_id=%s key=%s",
                collection_id,
                previous_key,
            )

    return CollectionHeroResponse.model_validate(collection)


@router.patch(
    "/{collection_id}/hero", dependencies=[_require], response_model=CollectionHeroResponse
)
async def update_collection_hero_alt(
    collection_id: int,
    body: CollectionHeroAltUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Update only the alt text on the hero image (admin)."""
    collection = crud_collection.get(db, id=collection_id)
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if collection.hero_image_url is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Collection has no hero image. Upload one before editing alt text.",
        )

    collection.hero_image_alt = body.hero_image_alt
    audit.record_update(resource_type="collection", obj=collection)
    db.commit()
    db.refresh(collection)
    return CollectionHeroResponse.model_validate(collection)


@router.delete(
    "/{collection_id}/hero", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_collection_hero(
    collection_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Clear the hero columns and best-effort delete the R2 object (admin)."""
    collection = crud_collection.get(db, id=collection_id)
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if collection.hero_image_url is None:
        return None  # idempotent

    r2 = get_r2_client()
    key = _r2_key_from_hero_url(collection.hero_image_url)
    if key and not delete_from_r2(r2, bucket=settings.R2_BUCKET_NAME, key=key):
        logger.warning(
            "R2 delete failed for collection_id=%s key=%s; columns cleared anyway",
            collection_id,
            key,
        )

    collection.hero_image_url = None
    collection.hero_image_alt = None
    collection.hero_width = None
    collection.hero_height = None
    collection.hero_bytes = None
    collection.hero_mime = None
    audit.record_update(resource_type="collection", obj=collection)
    db.commit()
    return None
