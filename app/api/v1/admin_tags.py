"""Admin tag-vocabulary API (/api/v1/admin/tags).

CRUD over the shared customer-tag vocabulary. Admin-gated; every mutation is
audited as ``resource_type="tag"``. Tag *assignment* to a customer lives under
``/api/v1/admin/customers/{id}/tags`` (see ``admin_customers.py``).

Gated on ``customers:*``, not ``catalog:*`` — these are customer tags, and the
vocabulary is fetched by ``/admin/customers`` itself. It was catalog-gated from
the original capability rollout through BE #242, which meant a customers
staffer's own index page 403'd on the tag chips. The *product*-tag vocabulary
is a separate router (``admin_product_tags.py``) and stays on ``catalog:*``.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.capabilities import CUSTOMERS_READ, CUSTOMERS_WRITE
from app.core.dependencies import get_current_admin, get_db, require_capability, require_read
from app.crud import tag as crud_tag
from app.models.tag import Tag
from app.models.user import User
from app.schemas.tag import TagCreate, TagResponse, TagUpdate
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

# Capability gate for this router's write/DELETE routes.
_require = Depends(require_capability(CUSTOMERS_WRITE))
_read = Depends(require_read(CUSTOMERS_READ))


def _response(tag: Tag, usage_count: int) -> TagResponse:
    return TagResponse(
        id=tag.id,
        created_at=tag.created_at,
        updated_at=tag.updated_at,
        name=tag.name,
        color=tag.color,
        usage_count=usage_count,
    )


@router.get("/", response_model=list[TagResponse])
async def list_tags(
    db: Session = Depends(get_db),
    current_admin: User = _read,
):
    """List the tag vocabulary with per-tag customer usage counts (admin)."""
    return [_response(t, count) for t, count in crud_tag.list_with_counts(db)]


@router.post(
    "/", dependencies=[_require], response_model=TagResponse, status_code=status.HTTP_201_CREATED
)
async def create_tag(
    payload: TagCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a tag (admin). 409 if the name already exists (case-insensitive)."""
    if crud_tag.get_by_name_ci(db, name=payload.name) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A tag with this name already exists"
        )
    tag = crud_tag.create_tag(db, name=payload.name, color=payload.color)
    audit.record_create(resource_type="tag", obj=tag)
    db.commit()
    db.refresh(tag)
    return _response(tag, 0)


@router.patch("/{tag_id}", dependencies=[_require], response_model=TagResponse)
async def update_tag(
    tag_id: int,
    payload: TagUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Rename / recolor a tag (admin). 409 if a rename collides with another tag."""
    tag = crud_tag.get(db, id=tag_id)
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")

    if payload.name is not None:
        clash = crud_tag.get_by_name_ci(db, name=payload.name)
        if clash is not None and clash.id != tag.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="A tag with this name already exists"
            )

    crud_tag.update(db, db_obj=tag, obj_in=payload)
    audit.record_update(resource_type="tag", obj=tag)
    db.commit()
    db.refresh(tag)
    return _response(tag, crud_tag.usage_count(db, tag_id=tag.id))


@router.delete("/{tag_id}", dependencies=[_require], status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(
    tag_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a tag (admin). Cascades to remove it from every customer."""
    tag = crud_tag.get(db, id=tag_id)
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
    audit.record_delete(resource_type="tag", obj=tag)
    db.delete(tag)
    db.commit()
