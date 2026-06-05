"""Seed the ``home`` CMS page (the storefront homepage renders it via PageRenderer).

    python -m app.scripts.seed_homepage_page

Idempotent (get-or-create on slug+locale): creates one published ``home`` page per
locale (``en`` + ``pl``) with a minimal, image-free layout:

    promo-strip  →  hero-banners (collection-list)  →  product-grid (featured)

Nothing here ships images — the storefront renders a generated placeholder for
any image block whose image hasn't been uploaded yet, and an admin fills in real
content (including a hero/slideshow) from the page editor. On rerun, existing
pages are LEFT UNTOUCHED so an admin's edits survive — only a missing locale row
is inserted. Exits 0 on success; non-zero on error.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.crud import page as crud_page
from app.models.page import PageStatus, PageType
from app.schemas.page import PageLayout

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("seed_homepage_page")


_FEATURED_HEADING = {"en": "Featured products", "pl": "Polecane produkty"}
_SEO_TITLE = {"en": "Home", "pl": "Strona główna"}


def _build_layout(locale: str) -> dict[str, Any]:
    """Build the minimal homepage section order as a validated layout blob."""
    layout = PageLayout.model_validate(
        {
            "sections": [
                # 1) promo strip (empty — an admin can add a promo message)
                {"id": "promo-strip", "type": "promo-strip", "settings": {"text": ""}},
                # 2) hero banners (empty collection-list — an admin can add tiles)
                {
                    "id": "hero-banners",
                    "type": "collection-list",
                    "settings": {"columns": 3},
                    "blocks": [],
                },
                # 3) featured product grid
                {
                    "id": "featured-grid",
                    "type": "product-grid",
                    "settings": {
                        "heading": _FEATURED_HEADING[locale],
                        "source": "featured",
                        "columns": 4,
                        "limit": 8,
                    },
                },
            ]
        }
    )
    return layout.model_dump(mode="json")


def _seed_locale(db: Session, *, locale: str) -> bool:
    """Create the published ``home`` page for ``locale`` if absent. Returns True
    if a new row was inserted, False if it already existed."""
    existing = crud_page.get_by_slug_locale(db, slug="home", locale=locale)
    if existing is not None:
        logger.info("home/%s already exists (id=%s) — left untouched", locale, existing.id)
        return False

    blob = _build_layout(locale)
    page = crud_page.create(
        db,
        slug="home",
        type=PageType.HOME.value,
        locale=locale,
        draft_layout=blob,
        seo_title=_SEO_TITLE[locale],
        seo_description=None,
        seo_image_url=None,
        created_by=None,
    )
    # Publish immediately so the storefront serves it.
    crud_page.publish(db, page=page, label="seed", actor_id=None)
    page.status = PageStatus.PUBLISHED.value
    db.commit()
    logger.info("seeded + published home/%s (id=%s)", locale, page.id)
    return True


def main() -> int:
    db = SessionLocal()
    try:
        for locale in ("en", "pl"):
            _seed_locale(db, locale=locale)
        return 0
    except Exception:  # top-level script guard, fail loudly
        logger.exception("homepage seed failed")
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
