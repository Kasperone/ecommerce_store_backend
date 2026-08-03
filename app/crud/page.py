"""CMS page CRUD + preview-token signing.

Mirrors ``CRUDBanner``: **no commit anywhere** — the route owns the transaction so
a mutation and its audit row land (or roll back) together. ``create``/``save_draft``
flush so ``id`` is populated for the audit writer; ``publish`` snapshots a
``page_versions`` row and swaps it into ``published_layout``.

Preview tokens are short-TTL HMAC tokens (``itsdangerous``-free, stdlib ``hmac`` +
``settings.SECRET_KEY``) bound to ``{pid, exp}`` — the page id and an expiry epoch.
``verify_preview_token`` returns ``False`` for anything bad/expired/tampered, and
the route turns that into a 404 rather than a 401/403 (anti-enumeration: a preview
link never confirms a page exists).

A token is **not** bound to the draft's content or version, so editing the draft
does not invalidate an outstanding link — within its TTL the link keeps serving
whatever the draft currently is. That is deliberate: ``onPreviewDraft`` in the
admin editor flushes the draft *before* minting, i.e. the feature is "show me my
working draft", not "snapshot this instant". Exposure stays bounded by the TTL
below, a single page id, and draft content only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.page import Page, PageStatus, PageVersion

# Preview tokens live ~15 minutes.
PREVIEW_TOKEN_TTL_SECONDS = 15 * 60


def _sign(message: bytes) -> str:
    sig = hmac.new(settings.SECRET_KEY.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(token: str) -> bytes:
    padding = "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(token + padding)


def make_preview_token(*, page_id: int, ttl_seconds: int = PREVIEW_TOKEN_TTL_SECONDS) -> str:
    """Mint a signed preview token bound to ``page_id`` with an expiry epoch."""
    payload = {"pid": page_id, "exp": int(time.time()) + ttl_seconds}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_sign(body.encode('ascii'))}"


def verify_preview_token(token: str, *, page_id: int) -> bool:
    """Return True iff ``token`` is a valid, unexpired token for ``page_id``.

    Constant-time signature compare; any malformed/expired/mismatched token
    returns False (the route turns that into a 404).
    """
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return False
    if not hmac.compare_digest(sig, _sign(body.encode("ascii"))):
        return False
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return False
    if payload.get("pid") != page_id:
        return False
    exp = payload.get("exp")
    return isinstance(exp, int) and exp >= int(time.time())


class CRUDPage:
    """Page rows (no commit; caller owns the txn)."""

    # ---- reads ----------------------------------------------------------- #

    def get(self, db: Session, id: int) -> Page | None:
        return db.get(Page, id)

    def get_by_slug_locale(self, db: Session, *, slug: str, locale: str) -> Page | None:
        return db.query(Page).filter(Page.slug == slug, Page.locale == locale).one_or_none()

    def get_published(self, db: Session, *, slug: str, locale: str) -> Page | None:
        """Public read: a published page with a non-null published layout."""
        return (
            db.query(Page)
            .filter(
                Page.slug == slug,
                Page.locale == locale,
                Page.status == PageStatus.PUBLISHED.value,
                Page.published_layout.isnot(None),
            )
            .one_or_none()
        )

    def get_multi(
        self,
        db: Session,
        *,
        locale: str | None = None,
        status: str | None = None,
        type: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[Page]:
        query = self._filtered(db, locale=locale, status=status, type=type)
        return (
            query.order_by(Page.updated_at.desc(), Page.id.desc()).offset(skip).limit(limit).all()
        )

    def count(
        self,
        db: Session,
        *,
        locale: str | None = None,
        status: str | None = None,
        type: str | None = None,
    ) -> int:
        return self._filtered(db, locale=locale, status=status, type=type).count()

    def _filtered(self, db: Session, *, locale: str | None, status: str | None, type: str | None):
        query = db.query(Page)
        if locale is not None:
            query = query.filter(Page.locale == locale)
        if status is not None:
            query = query.filter(Page.status == status)
        if type is not None:
            query = query.filter(Page.type == type)
        return query

    def list_versions(self, db: Session, *, page_id: int) -> list[PageVersion]:
        return (
            db.query(PageVersion)
            .filter(PageVersion.page_id == page_id)
            .order_by(PageVersion.version_no.desc())
            .all()
        )

    def get_version(self, db: Session, *, page_id: int, version_id: int) -> PageVersion | None:
        return (
            db.query(PageVersion)
            .filter(PageVersion.id == version_id, PageVersion.page_id == page_id)
            .one_or_none()
        )

    # ---- writes (flush, no commit) --------------------------------------- #

    def create(
        self,
        db: Session,
        *,
        slug: str,
        type: str,
        locale: str,
        draft_layout: dict[str, Any],
        seo_title: str | None,
        seo_description: str | None,
        seo_image_url: str | None,
        created_by: int | None,
    ) -> Page:
        page = Page(
            slug=slug,
            type=type,
            locale=locale,
            status=PageStatus.DRAFT.value,
            draft_layout=draft_layout,
            published_layout=None,
            seo_title=seo_title,
            seo_description=seo_description,
            seo_image_url=seo_image_url,
            created_by=created_by,
        )
        db.add(page)
        db.flush()
        db.refresh(page)
        return page

    def save_draft(
        self,
        db: Session,
        *,
        page: Page,
        draft_layout: dict[str, Any] | None = None,
        seo: dict[str, Any] | None = None,
    ) -> Page:
        """Update the working draft + optional SEO (no flush — the audit writer
        reads ``get_history`` between this and the caller's commit)."""
        if draft_layout is not None:
            page.draft_layout = draft_layout
        if seo:
            for field, value in seo.items():
                setattr(page, field, value)
        db.add(page)
        return page

    def publish(
        self,
        db: Session,
        *,
        page: Page,
        label: str | None,
        actor_id: int | None,
    ) -> PageVersion:
        """Snapshot the current draft into a new immutable ``page_versions`` row,
        then swap it into ``published_layout`` + flip status to ``published``.

        Returns the new version row. No commit — the caller owns the txn.
        """
        next_no = (
            db.query(func.coalesce(func.max(PageVersion.version_no), 0))
            .filter(PageVersion.page_id == page.id)
            .scalar()
            or 0
        ) + 1
        version = PageVersion(
            page_id=page.id,
            version_no=next_no,
            layout=page.draft_layout,
            label=label,
            created_by=actor_id,
        )
        db.add(version)
        db.flush()  # populate version.id for the FK swap below

        page.published_layout = page.draft_layout
        page.published_version_id = version.id
        page.status = PageStatus.PUBLISHED.value
        db.add(page)
        return version

    def rollback(self, db: Session, *, page: Page, version: PageVersion) -> Page:
        """Restore a prior version's layout into the page's draft (admin then
        re-publishes). The version rows themselves are never mutated."""
        page.draft_layout = version.layout
        db.add(page)
        return page

    def delete(self, db: Session, *, page: Page) -> None:
        db.delete(page)


page = CRUDPage()
