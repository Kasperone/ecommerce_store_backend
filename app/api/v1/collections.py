"""Public collection read endpoints (/api/v1/collections).

Storefront-facing: a list of active collections (with member product counts) and
a slug-resolved detail (EN+PL). Products *in* a collection are served by the
existing product list via its ``collection_id`` filter — no separate paginated
endpoint, mirroring how the category page reuses the product list.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_db
from app.crud import collection as crud_collection
from app.schemas.collection import CollectionResponse, CollectionWithProductCount

router = APIRouter()


@router.get("/", response_model=list[CollectionWithProductCount])
async def list_collections(
    db: Session = Depends(get_db),
    skip: int = Query(0, ge=0, description="Offset for pagination"),
    limit: int = Query(100, ge=1, le=100, description="Limit for pagination"),
):
    """List active collections with member product counts."""
    collections = crud_collection.get_active(db, skip=skip, limit=limit)
    counts = crud_collection.product_counts(db)
    result = []
    for col in collections:
        col_dict = CollectionResponse.model_validate(col).model_dump()
        col_dict["product_count"] = counts.get(col.id, 0)
        result.append(col_dict)
    return result


@router.get("/slug/{slug}", response_model=CollectionWithProductCount)
async def get_collection_by_slug(
    slug: str,
    db: Session = Depends(get_db),
):
    """Get an active collection by slug (EN or PL), with its product count.

    Searches both ``slug`` (EN) and ``slug_pl`` (PL); the EN row wins on a
    cross-locale collision. 404 when missing or inactive.
    """
    collection = crud_collection.get_by_any_slug(db, slug=slug)
    if not collection or not collection.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")

    col_dict = CollectionResponse.model_validate(collection).model_dump()
    col_dict["product_count"] = crud_collection.count_products(db, collection_id=collection.id)
    return col_dict
