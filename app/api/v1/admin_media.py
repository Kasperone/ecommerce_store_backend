"""Admin media upload — a stateless R2 image store for the CMS page builder.

Unlike the product-image route (``uploads.py``), this endpoint has no parent
entity and writes no DB row: it validates the bytes, stores the object under a
``cms/`` key prefix, audits the upload, and returns the public URL plus
dimensions/size/mime. Orphan cleanup is acceptable for V1 — the trade-off of a
stateless object store with no media-library table (a deliberate decision; see
the CMS findings plan, F2).

Mounted at ``/api/v1/admin/media`` in ``app/main.py``.
"""

import uuid

from fastapi import APIRouter, Depends, File, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.capabilities import CATALOG_WRITE
from app.core.config import settings
from app.core.dependencies import get_current_admin, get_db, require_capability
from app.core.storage import get_r2_client
from app.models.user import User
from app.services.audit import AuditWriter, get_audit_writer
from app.services.image import (
    MIME_TO_EXTENSION,
    capture_dimensions,
    read_with_size_cap,
    upload_to_r2,
)

router = APIRouter()

# Capability gate for this router's write/DELETE routes.
_require = Depends(require_capability(CATALOG_WRITE))


class MediaUploadResponse(BaseModel):
    """The stored object's public URL plus its captured metadata."""

    url: str = Field(description="Public R2 URL of the stored image.")
    width: int = Field(description="Image width in pixels.")
    height: int = Field(description="Image height in pixels.")
    bytes: int = Field(description="Stored object size in bytes.")
    mime: str = Field(description="Detected (magic-byte) MIME type.")


@router.post(
    "",
    dependencies=[_require],
    response_model=MediaUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_media(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Upload an image to R2 and return its public URL (Admin only, stateless).

    No DB row is created — the URL is the only persisted reference (CMS layouts
    store it as a plain string). Used by the page-builder image picker.
    """
    # Validate the bytes BEFORE asking for an R2 client. The magic-byte sniff in
    # ``read_with_size_cap`` is the cheapest reject path — a spoofed extension
    # never reaches the R2 dep, which keeps the error a 400 ("Unsupported image
    # format") instead of a 503 ("R2 not configured") when the env lacks creds.
    # Mirrors the ordering of the product-image upload route.
    body, detected_mime = await read_with_size_cap(file)
    width, height = capture_dimensions(body)

    r2 = get_r2_client()

    ext = MIME_TO_EXTENSION[detected_mime]
    key = f"cms/{uuid.uuid4().hex}.{ext}"
    public_url = upload_to_r2(
        r2,
        bucket=settings.R2_BUCKET_NAME,
        key=key,
        body=body,
        content_type=detected_mime,
    )

    # Stateless object — no row to point a resource_id at. Audit the upload as a
    # ``media`` create-event keyed by the object URL for traceability.
    audit.record_event(
        action="create",
        resource_type="media",
        resource_id=None,
        payload={"url": public_url, "key": key, "bytes": len(body), "mime": detected_mime},
    )
    db.commit()

    return MediaUploadResponse(
        url=public_url,
        width=width,
        height=height,
        bytes=len(body),
        mime=detected_mime,
    )
