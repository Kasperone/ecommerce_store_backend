"""Admin product-tag vocabulary API (/api/v1/admin/product-tags).

CRUD over the shared product-tag merchandising vocabulary. Admin-gated; every
mutation is audited as ``resource_type="product_tag"``. Tag *assignment* to a
product happens on the product writes (``POST``/``PUT /api/v1/products``) via the
``tags`` field — see ``products.py``.

Mirrors ``admin_tags.py`` (customer tags) with strictly distinct ``product_tag``
naming so the two systems never collide.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.capabilities import CATALOG_WRITE
from app.core.dependencies import get_current_admin, get_db, require_capability
from app.crud import product_tag as crud_product_tag
from app.models.product_tag import ProductTag
from app.models.user import User
from app.schemas.product_tag import ProductTagCreate, ProductTagResponse, ProductTagUpdate
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

# Capability gate for this router's write/DELETE routes.
_require = Depends(require_capability(CATALOG_WRITE))


def _response(tag: ProductTag, usage_count: int) -> ProductTagResponse:
    return ProductTagResponse(
        id=tag.id,
        created_at=tag.created_at,
        updated_at=tag.updated_at,
        name=tag.name,
        color=tag.color,
        usage_count=usage_count,
    )


@router.get("/", response_model=list[ProductTagResponse])
async def list_product_tags(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """List the product-tag vocabulary with per-tag product usage counts (admin)."""
    return [_response(t, count) for t, count in crud_product_tag.list_with_counts(db)]


@router.post(
    "/",
    dependencies=[_require],
    response_model=ProductTagResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_product_tag(
    payload: ProductTagCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a product tag (admin). 409 if the name already exists (case-insensitive)."""
    if crud_product_tag.get_by_name_ci(db, name=payload.name) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A product tag with this name already exists",
        )
    tag = crud_product_tag.create_tag(db, name=payload.name, color=payload.color)
    audit.record_create(resource_type="product_tag", obj=tag)
    db.commit()
    db.refresh(tag)
    return _response(tag, 0)


@router.patch("/{tag_id}", dependencies=[_require], response_model=ProductTagResponse)
async def update_product_tag(
    tag_id: int,
    payload: ProductTagUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Rename / recolor a product tag (admin). 409 if a rename collides with another tag."""
    tag = crud_product_tag.get(db, id=tag_id)
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product tag not found")

    if payload.name is not None:
        clash = crud_product_tag.get_by_name_ci(db, name=payload.name)
        if clash is not None and clash.id != tag.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A product tag with this name already exists",
            )

    crud_product_tag.update(db, db_obj=tag, obj_in=payload)
    audit.record_update(resource_type="product_tag", obj=tag)
    db.commit()
    db.refresh(tag)
    return _response(tag, crud_product_tag.usage_count(db, tag_id=tag.id))


@router.delete("/{tag_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT)
async def delete_product_tag(
    tag_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a product tag (admin). Cascades to remove it from every product."""
    tag = crud_product_tag.get(db, id=tag_id)
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product tag not found")
    audit.record_delete(resource_type="product_tag", obj=tag)
    db.delete(tag)
    db.commit()
