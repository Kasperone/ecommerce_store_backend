"""Admin pages API — full CMS page CRUD + draft/publish/version/rollback.

All routes are admin-gated and audited (resource_type ``page``). Layouts are
validated explicitly via ``PageLayout.model_validate`` (the FastAPI nested-union
caveat, research §3.4) so an unknown section ``type`` yields one clean 422 error.

``custom-html`` blocks are **server-sanitized with nh3** (allowlist) before the
layout is ever stored — the raw field is never trusted at render. Sanitization
runs on every draft-bearing mutation (create + draft save) so a draft persisted
once is safe forever.

Routes:
- ``GET    /admin/pages``                         list (paginated, filterable)
- ``POST   /admin/pages``                         create (audited)
- ``GET    /admin/pages/{id}``                    detail
- ``PATCH  /admin/pages/{id}/draft``              save draft layout/SEO (audited)
- ``POST   /admin/pages/{id}/publish``            snapshot version + swap (audited)
- ``POST   /admin/pages/{id}/preview-token``      mint a signed preview token
- ``GET    /admin/pages/{id}/versions``           list immutable versions
- ``POST   /admin/pages/{id}/versions/{vid}/rollback``  restore a version (audited)
- ``DELETE /admin/pages/{id}``                    delete (audited)
"""

from math import ceil
from typing import Any

import nh3
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_admin, get_db
from app.crud import page as crud_page
from app.crud.page import PREVIEW_TOKEN_TTL_SECONDS, make_preview_token
from app.models.user import User
from app.schemas.page import (
    PageCreate,
    PageDraftUpdate,
    PageLayout,
    PageListResponse,
    PageLocaleLiteral,
    PagePublishRequest,
    PageResponse,
    PageStatusLiteral,
    PageTypeLiteral,
    PageVersionListResponse,
    PreviewTokenResponse,
)
from app.services.audit import AuditWriter, get_audit_writer

router = APIRouter()

# nh3 (Rust ammonia) allowlist for the custom-html block. Defaults already drop
# <script>/<style> and event handlers; we explicitly allow a pragmatic content
# set and strip every other tag's markup (keeping text).
_HTML_ALLOWED_TAGS: set[str] = {
    "a",
    "abbr",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "section",
    "small",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
_HTML_ALLOWED_ATTRS: dict[str, set[str]] = {
    # nh3 manages ``rel`` on <a> itself (adds noopener/noreferrer) — don't list it.
    "a": {"href", "title", "target"},
    "img": {"src", "alt", "title", "width", "height", "loading"},
    "*": {"class"},
}


def _sanitize_custom_html(layout: PageLayout) -> dict[str, Any]:
    """Validate-then-sanitize a layout: return the JSON-safe dict with every
    ``custom-html`` block's ``html`` run through nh3."""
    blob = layout.model_dump(mode="json")
    for section in blob.get("sections", []):
        if section.get("type") == "custom-html":
            raw = section.get("settings", {}).get("html", "")
            section["settings"]["html"] = nh3.clean(
                raw or "",
                tags=_HTML_ALLOWED_TAGS,
                attributes=_HTML_ALLOWED_ATTRS,
            )
    return blob


def _validate_layout(raw: Any) -> PageLayout:
    """Explicitly validate a layout (research §3.4). 422 with a clean error on an
    unknown section type or malformed settings."""
    try:
        return PageLayout.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(include_url=False),
        ) from exc


def _get_or_404(db: Session, page_id: int):
    page = crud_page.get(db, page_id)
    if page is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    return page


# --------------------------------------------------------------------------- #
# list / create                                                               #
# --------------------------------------------------------------------------- #


@router.get("/", response_model=PageListResponse)
async def list_pages(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    locale: PageLocaleLiteral | None = Query(None, description="Filter by locale"),
    status_filter: PageStatusLiteral | None = Query(
        None, alias="status", description="Filter by status"
    ),
    type_filter: PageTypeLiteral | None = Query(
        None, alias="type", description="Filter by page type"
    ),
):
    """List pages (newest-edited first), filterable by locale/status/type."""
    skip = (page - 1) * page_size
    pages = crud_page.get_multi(
        db, locale=locale, status=status_filter, type=type_filter, skip=skip, limit=page_size
    )
    total = crud_page.count(db, locale=locale, status=status_filter, type=type_filter)
    total_pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": pages,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": total_pages,
    }


@router.post("/", response_model=PageResponse, status_code=status.HTTP_201_CREATED)
async def create_page(
    payload: PageCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Create a draft page. ``custom-html`` blocks are sanitized before storage."""
    draft_blob = _sanitize_custom_html(payload.draft_layout)
    try:
        page = crud_page.create(
            db,
            slug=payload.slug,
            type=payload.type,
            locale=payload.locale,
            draft_layout=draft_blob,
            seo_title=payload.seo_title,
            seo_description=payload.seo_description,
            seo_image_url=payload.seo_image_url,
            created_by=current_admin.id,
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A page with this slug and locale already exists",
        ) from exc
    audit.record_create(resource_type="page", obj=page)
    db.commit()
    db.refresh(page)
    return page


# --------------------------------------------------------------------------- #
# detail / draft / publish                                                     #
# --------------------------------------------------------------------------- #


@router.get("/{page_id}", response_model=PageResponse)
async def get_page(
    page_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Get one page by id (draft + published)."""
    return _get_or_404(db, page_id)


@router.patch("/{page_id}/draft", response_model=PageResponse)
async def save_draft(
    page_id: int,
    payload: PageDraftUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Save the working draft layout and/or SEO. The layout is validated +
    sanitized; an unknown section type 422s."""
    page = _get_or_404(db, page_id)

    draft_blob: dict[str, Any] | None = None
    if payload.draft_layout is not None:
        draft_blob = _sanitize_custom_html(payload.draft_layout)

    seo = payload.model_dump(
        exclude_unset=True, include={"seo_title", "seo_description", "seo_image_url"}
    )

    crud_page.save_draft(db, page=page, draft_layout=draft_blob, seo=seo or None)
    audit.record_update(resource_type="page", obj=page)
    db.commit()
    db.refresh(page)
    return page


@router.post("/{page_id}/publish", response_model=PageResponse)
async def publish_page(
    page_id: int,
    payload: PagePublishRequest | None = None,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Validate the current draft, snapshot it into a version, and swap it into
    the published layout (status → published)."""
    page = _get_or_404(db, page_id)
    # Re-validate the stored draft before going live (defends against a draft
    # written before a schema change, or a direct DB edit).
    _validate_layout(page.draft_layout)

    label = payload.label if payload is not None else None
    version = crud_page.publish(db, page=page, label=label, actor_id=current_admin.id)
    audit.record_event(
        action="publish",
        resource_type="page",
        resource_id=page.id,
        payload={"version_id": version.id, "version_no": version.version_no, "label": label},
    )
    db.commit()
    db.refresh(page)
    return page


# --------------------------------------------------------------------------- #
# preview token                                                                #
# --------------------------------------------------------------------------- #


@router.post(
    "/{page_id}/preview-token",
    response_model=PreviewTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_preview_token(
    page_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """Mint a short-lived signed preview token for this page's draft layout.

    Not audited (read-only, no data change). The token is consumed by the public
    ``GET /pages/{slug}/preview?token=`` route.
    """
    from datetime import UTC, datetime, timedelta

    page = _get_or_404(db, page_id)
    token = make_preview_token(page_id=page.id)
    expires_at = datetime.now(UTC) + timedelta(seconds=PREVIEW_TOKEN_TTL_SECONDS)
    return PreviewTokenResponse(token=token, expires_at=expires_at)


# --------------------------------------------------------------------------- #
# versions / rollback                                                          #
# --------------------------------------------------------------------------- #


@router.get("/{page_id}/versions", response_model=PageVersionListResponse)
async def list_versions(
    page_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    """List a page's immutable versions, newest first."""
    _get_or_404(db, page_id)
    versions = crud_page.list_versions(db, page_id=page_id)
    return {"items": versions, "total": len(versions)}


@router.post("/{page_id}/versions/{version_id}/rollback", response_model=PageResponse)
async def rollback_version(
    page_id: int,
    version_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Restore a prior version's layout into the page's draft (admin re-publishes
    to take it live). The version rows themselves are never mutated."""
    page = _get_or_404(db, page_id)
    version = crud_page.get_version(db, page_id=page_id, version_id=version_id)
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    crud_page.rollback(db, page=page, version=version)
    audit.record_event(
        action="rollback",
        resource_type="page",
        resource_id=page.id,
        payload={"version_id": version.id, "version_no": version.version_no},
    )
    db.commit()
    db.refresh(page)
    return page


# --------------------------------------------------------------------------- #
# delete                                                                       #
# --------------------------------------------------------------------------- #


@router.delete("/{page_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_page(
    page_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """Delete a page (cascades its versions)."""
    page = _get_or_404(db, page_id)
    audit.record_delete(resource_type="page", obj=page)
    crud_page.delete(db, page=page)
    db.commit()
