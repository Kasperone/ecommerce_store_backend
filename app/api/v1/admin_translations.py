"""Admin Translations workspace API (Phase 1).

A registry-driven, Shopify-"Translate & Adapt"-style surface over the existing
per-locale ``*_pl`` columns. Lists translatable resources by type with the
English source beside the translation, reports completeness, and lets admins
fill gaps — without ever enforcing translation at resource-creation time.

All routes admin-gated. The update is audited (``resource_type`` = the
translatable type, e.g. ``shipping_method``) in the route's transaction.
The completeness audit CLI (``app.scripts.audit_translations``) and the data
model live alongside in :mod:`app.services.translation_registry`.
"""

from __future__ import annotations

from math import ceil
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_admin, get_db
from app.models.user import User
from app.schemas.translation import (
    TranslationResourceListResponse,
    TranslationResourceRow,
    TranslationResourceTypesResponse,
    TranslationUpdateRequest,
)
from app.services.audit import AuditWriter, get_audit_writer
from app.services.translation_admin import (
    FieldTooLongError,
    UnknownFieldError,
    UnsupportedLocaleError,
    build_resource_row,
    get_field_digests,
    get_resource_def,
    list_resources,
    summarize_types,
    update_resource,
    validate_locale,
)

router = APIRouter()


def _resolve_locale(locale: str) -> str:
    """Validate the locale query param, mapping unsupported to a 400."""
    try:
        validate_locale(locale)
    except UnsupportedLocaleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return locale


def _resolve_resource_def(resource_type: str):
    """Look up the resource def, mapping an unknown type to a 404."""
    try:
        return get_resource_def(resource_type)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# NOTE: ``/resource-types`` is declared before ``/{resource_type}`` so the
# static path is matched first (FastAPI resolves routes in declaration order).
@router.get("/resource-types", response_model=TranslationResourceTypesResponse)
async def list_resource_types(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    locale: str = Query("pl"),
):
    """Registry summary + per-type completeness counts for ``locale`` (admin)."""
    _resolve_locale(locale)
    return summarize_types(db, locale)


@router.get("/{resource_type}", response_model=TranslationResourceListResponse)
async def list_translations(
    resource_type: str,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    locale: str = Query("pl"),
    status_filter: Literal["all", "missing", "outdated"] = Query("all", alias="status"),
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    """List a resource type's rows with source + translation + completeness (admin)."""
    _resolve_locale(locale)
    resource_def = _resolve_resource_def(resource_type)
    items, total = list_resources(
        db,
        resource_def=resource_def,
        locale=locale,
        status=status_filter,
        q=q,
        page=page,
        page_size=page_size,
    )
    pages = ceil(total / page_size) if total > 0 else 1
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "locale": locale,
        "resource_type": resource_type,
    }


@router.put("/{resource_type}/{resource_id}", response_model=TranslationResourceRow)
async def update_translation(
    resource_type: str,
    resource_id: int,
    payload: TranslationUpdateRequest,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
    locale: str = Query("pl"),
):
    """Write translations for one resource's fields in ``locale`` (admin)."""
    _resolve_locale(locale)
    resource_def = _resolve_resource_def(resource_type)
    try:
        instance = update_resource(
            db,
            resource_def=resource_def,
            resource_id=resource_id,
            locale=locale,
            fields=payload.fields,
        )
    except UnknownFieldError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except FieldTooLongError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except UnsupportedLocaleError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if instance is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{resource_type} #{resource_id} not found",
        )

    audit.record_update(resource_type=resource_type, obj=instance)
    db.commit()
    db.refresh(instance)
    digests = get_field_digests(
        db, resource_type=resource_type, resource_id=resource_id, locale=locale
    )
    return build_resource_row(instance, resource_def, locale, digests=digests)
