"""CMS page-builder models — ``Page`` + ``PageVersion``.

A ``Page`` is the Shopify-Online-Store-2.0-style "section system" unit: an ordered
list of typed **sections** stored as a JSONB ``layout`` blob and validated by a
Pydantic v2 discriminated union (see ``app/schemas/page.py``). Admins edit
``draft_layout``; the storefront serves ``published_layout``. **Publish** snapshots
the draft into an append-only ``page_versions`` row and swaps it into
``published_layout`` — rollback is a one-row copy.

Conventions mirror ``Banner``:

- ``type`` / ``locale`` / ``status`` are ``str``-enums stored as the lowercase value
  in a plain ``String`` column (NOT a PG enum), each guarded by a CHECK constraint.
  Adding a value later is a code-only change with no migration.
- JSONB columns use ``postgresql.JSONB`` (the audit-events table is the template).
- Image fields inside block settings store a plain ``image_url: str | None`` for V1
  (parity with ``Banner.image_url``); an R2 asset-ref/key picker is a later additive
  change.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class PageType(str, enum.Enum):
    """The kind of page. ``home`` is the storefront homepage; ``standard`` a
    generic CMS page (catch-all route); ``landing`` a marketing landing page."""

    HOME = "home"
    STANDARD = "standard"
    LANDING = "landing"


class PageLocale(str, enum.Enum):
    """Page locale. Pages are per-locale rows sharing a slug (structural locale
    independence, aligned with the ``/pl/`` routing)."""

    EN = "en"
    PL = "pl"


class PageStatus(str, enum.Enum):
    """Publication state. ``scheduled`` is reserved for the (additive) scheduling
    worker — V1 ships ``draft`` / ``published`` and tolerates the third value."""

    DRAFT = "draft"
    PUBLISHED = "published"
    SCHEDULED = "scheduled"


# Mirrors of the ``*.values`` used by the DB CHECK constraints and cross-checked
# by ``app/schemas/page.py`` at import so the route, DB, and OpenAPI layers can't
# drift apart. Mirrors ``BANNER_PLACEMENT_VALUES``.
PAGE_TYPE_VALUES: tuple[str, ...] = tuple(t.value for t in PageType)
PAGE_LOCALE_VALUES: tuple[str, ...] = tuple(t.value for t in PageLocale)
PAGE_STATUS_VALUES: tuple[str, ...] = tuple(t.value for t in PageStatus)


class Page(BaseModel):
    """A composable storefront page.

    ``slug`` + ``locale`` are unique together (one row per locale per slug).
    ``draft_layout`` is the working copy admins edit; ``published_layout`` is what
    the storefront serves (null until first publish). ``published_version_id``
    points at the ``page_versions`` row currently live. ``scheduled_at`` /
    ``unpublish_at`` are an optional display window for the scheduling worker.
    SEO fields are page-level (not buried in a block). ``created_by`` is the admin
    who created the page (SET NULL on user delete).
    """

    __tablename__ = "pages"
    __table_args__ = (
        UniqueConstraint("slug", "locale", name="uq_pages_slug_locale"),
        CheckConstraint(
            "type IN ('" + "', '".join(PAGE_TYPE_VALUES) + "')",
            name="ck_pages_type_enum",
        ),
        CheckConstraint(
            "locale IN ('" + "', '".join(PAGE_LOCALE_VALUES) + "')",
            name="ck_pages_locale_enum",
        ),
        CheckConstraint(
            "status IN ('" + "', '".join(PAGE_STATUS_VALUES) + "')",
            name="ck_pages_status_enum",
        ),
    )

    slug: Mapped[str] = mapped_column(String(255), index=True)
    type: Mapped[str] = mapped_column(String(16), default=PageType.STANDARD.value)
    locale: Mapped[str] = mapped_column(String(5))
    status: Mapped[str] = mapped_column(
        String(16), default=PageStatus.DRAFT.value, server_default="draft"
    )
    # The working copy admins edit; always present (defaults to an empty section
    # list at create). Stored as a JSONB object: ``{"sections": [...]}``.
    draft_layout: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB)
    published_layout: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB)
    published_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("page_versions.id", ondelete="SET NULL")
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)
    unpublish_at: Mapped[datetime | None] = mapped_column(DateTime)
    seo_title: Mapped[str | None] = mapped_column(String(255))
    seo_description: Mapped[str | None] = mapped_column(String(500))
    seo_image_url: Mapped[str | None] = mapped_column(String(2048))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    versions: Mapped[list[PageVersion]] = relationship(
        "PageVersion",
        back_populates="page",
        cascade="all, delete-orphan",
        order_by="PageVersion.version_no",
        foreign_keys="PageVersion.page_id",
    )

    def __repr__(self) -> str:
        return f"<Page {self.id} {self.slug!r} {self.locale!r} {self.status!r}>"


class PageVersion(BaseModel):
    """An immutable, append-only snapshot of a page's published layout.

    Written at publish time (and never mutated). ``version_no`` is monotonic per
    page. ``label`` is an optional admin-supplied note. Rollback copies a prior
    version's ``layout`` back into the page's ``draft_layout`` (admin then
    re-publishes) — the version rows themselves are never edited.
    """

    __tablename__ = "page_versions"

    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    layout: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB)
    label: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    page: Mapped[Page] = relationship("Page", back_populates="versions", foreign_keys=[page_id])

    def __repr__(self) -> str:
        return f"<PageVersion {self.id} page={self.page_id} v{self.version_no}>"
