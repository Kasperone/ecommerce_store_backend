"""Admin endpoints for product image upload, edit, delete.

Routes are nested under ``/api/v1/products/{product_id}/images`` — a product
image has no meaning without a parent. Mounted in ``app/main.py``.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.dependencies import get_current_admin, get_db
from app.core.storage import get_r2_client
from app.crud import product as crud_product
from app.crud import product_image as crud_product_image
from app.models.product_image import ProductImage
from app.models.user import User
from app.schemas.product import ProductImageResponse, ProductImageUpdate
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


def _get_image_for_product(db: Session, *, product_id: int, image_id: int) -> ProductImage:
    image = crud_product_image.get(db, id=image_id)
    if image is None or image.product_id != product_id:
        # 404 — don't leak existence of an image attached to a different product.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")
    return image


@router.post(
    "/{product_id}/images",
    response_model=ProductImageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_product_image(
    product_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Upload an image and attach it to a product (Admin only)."""
    product = crud_product.get(db, id=product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    # Validate the bytes BEFORE asking for an R2 client. The magic-byte sniff
    # in ``read_with_size_cap`` is the cheapest reject path — a spoofed
    # extension never reaches the R2 dep, which keeps the error a 400
    # ("Unsupported image format") instead of a 503 ("R2 not configured")
    # when the env is missing creds.
    body, detected_mime = await read_with_size_cap(file)
    width, height = capture_dimensions(body)

    r2 = get_r2_client()

    ext = MIME_TO_EXTENSION[detected_mime]
    key = f"products/{product_id}/{uuid.uuid4().hex}.{ext}"
    public_url = upload_to_r2(
        r2,
        bucket=settings.R2_BUCKET_NAME,
        key=key,
        body=body,
        content_type=detected_mime,
    )

    image = ProductImage(
        product_id=product_id,
        url=public_url,
        alt_text=None,
        sort_order=0,
        is_primary=False,
        width=width,
        height=height,
        bytes=len(body),
        mime=detected_mime,
    )
    db.add(image)
    db.flush()  # populate image.id before audit

    audit.record_create(resource_type="product_image", obj=image)
    db.commit()
    db.refresh(image)
    return image


@router.patch(
    "/{product_id}/images/{image_id}",
    response_model=ProductImageResponse,
)
async def update_product_image(
    product_id: int,
    image_id: int,
    image_in: ProductImageUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Edit metadata (alt text, sort order, primary flag) on an existing image."""
    image = _get_image_for_product(db, product_id=product_id, image_id=image_id)

    patch = image_in.model_dump(exclude_unset=True)

    # ``set_primary`` is the only safe path to promotion — it demotes siblings
    # in the same transaction so the partial unique index never sees two
    # primaries at once.
    if patch.get("is_primary") is True:
        crud_product_image.set_primary(db, product_id=product_id, image_id=image_id)
        patch.pop("is_primary")

    for key, value in patch.items():
        setattr(image, key, value)

    db.flush()
    audit.record_update(resource_type="product_image", obj=image)
    db.commit()
    db.refresh(image)
    return image


@router.delete(
    "/{product_id}/images/{image_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_product_image(
    product_id: int,
    image_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Remove an image row and its R2 object."""
    image = _get_image_for_product(db, product_id=product_id, image_id=image_id)

    r2 = get_r2_client()

    # Best-effort R2 cleanup. An R2 outage should not strand the DB row —
    # log and continue. Derive the key from the row's URL rather than
    # storing it separately.
    key = image.url.removeprefix(settings.R2_PUBLIC_URL).lstrip("/")
    if key and not delete_from_r2(r2, bucket=settings.R2_BUCKET_NAME, key=key):
        logger.warning(
            "R2 delete failed for product_id=%s image_id=%s key=%s; row removed anyway",
            product_id,
            image_id,
            key,
        )

    audit.record_delete(resource_type="product_image", obj=image)
    db.delete(image)
    db.commit()
    return None
