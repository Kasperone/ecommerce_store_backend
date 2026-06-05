"""Public pages API — the customer-facing render side of CMS pages.

Unauthenticated, like the public catalog. ``GET /pages/{slug}`` serves a page's
``published_layout`` for the resolved locale; a missing/unpublished page is a
**404** (never an empty 200 — the storefront catch-all distinguishes "no such
page" from "empty page"). ``GET /pages/{slug}/preview?token=`` serves the
``draft_layout`` for a valid signed preview token (404 on any bad/expired token,
anti-enumeration) and is marked no-cache.

Locale is resolved from an explicit ``?locale=`` query param, falling back to the
``Accept-Language`` header, then to ``en``.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_db
from app.crud import page as crud_page
from app.crud.page import verify_preview_token
from app.models.page import PAGE_LOCALE_VALUES
from app.schemas.page import PageLayout, PageLocaleLiteral, PagePublic

router = APIRouter()

_DEFAULT_LOCALE = "en"


def _resolve_locale(
    explicit: str | None,
    accept_language: str | None,
) -> str:
    """Pick a supported locale from ``?locale=`` then ``Accept-Language`` then en."""
    if explicit in PAGE_LOCALE_VALUES:
        return explicit  # type: ignore[return-value]
    if accept_language:
        # Take the highest-priority tag's primary subtag (e.g. "pl-PL,en;q=0.9").
        for part in accept_language.split(","):
            tag = part.split(";")[0].strip().lower()
            primary = tag.split("-")[0]
            if primary in PAGE_LOCALE_VALUES:
                return primary
    return _DEFAULT_LOCALE


def _to_public(page, layout_blob) -> PagePublic:
    return PagePublic(
        slug=page.slug,
        type=page.type,
        locale=page.locale,
        layout=PageLayout.model_validate(layout_blob),
        seo_title=page.seo_title,
        seo_description=page.seo_description,
        seo_image_url=page.seo_image_url,
    )


@router.get("/{slug}", response_model=PagePublic)
async def get_page(
    slug: str,
    db: Session = Depends(get_db),
    locale: PageLocaleLiteral | None = Query(
        None, description="Locale override; falls back to Accept-Language then 'en'."
    ),
    accept_language: str | None = Header(None),
):
    """Serve a published page's layout for the resolved locale (public, no auth)."""
    resolved = _resolve_locale(locale, accept_language)
    page = crud_page.get_published(db, slug=slug, locale=resolved)
    if page is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    return _to_public(page, page.published_layout)


@router.get("/{slug}/preview", response_model=PagePublic)
async def preview_page(
    slug: str,
    response: Response,
    token: str = Query(..., description="Signed preview token"),
    db: Session = Depends(get_db),
    locale: PageLocaleLiteral | None = Query(None),
    accept_language: str | None = Header(None),
):
    """Serve a page's *draft* layout for a valid signed preview token.

    404 on a missing page or any bad/expired/tampered token (anti-enumeration —
    a preview link never confirms a page exists). No-cache.
    """
    resolved = _resolve_locale(locale, accept_language)
    page = crud_page.get_by_slug_locale(db, slug=slug, locale=resolved)
    if page is None or not verify_preview_token(token, page_id=page.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page not found")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return _to_public(page, page.draft_layout)
