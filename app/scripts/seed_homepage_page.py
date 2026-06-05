"""Seed the ``home`` CMS page reproducing today's hardcoded homepage.

    python -m app.scripts.seed_homepage_page

Idempotent (get-or-create on slug+locale): creates one published ``home`` page per
locale (``en`` + ``pl``) whose layout reproduces the current ``index.vue``
composition — in section order:

    promo-strip  →  slideshow  →  hero-banners (collection-list)  →  product-grid (featured)

The slideshow seeds the three mock hero slides from
``frontend/shared/mocks/home.ts`` (localized copy); promo-strip + hero-banners
default to empty content (the live storefront fills them from the Banner API).
On rerun, existing pages are LEFT UNTOUCHED so an admin's edits survive — only a
missing locale row is inserted. Exits 0 on success; non-zero on error.
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


# Localized hero slides — mirror frontend/shared/mocks/home.ts HERO_SLIDES.
_SLIDES: dict[str, list[dict[str, Any]]] = {
    "en": [
        {
            "image_url": "/images/home/hero/summer-banner.jpg",
            "heading": "The growing season is here",
            "subheading": "Everything you need for a productive season.",
            "cta_label": "Shop products",
            "cta_url": "/products",
        },
        {
            "image_url": "/images/home/hero/sprayer-banner.jpg",
            "heading": "Crop protection that performs",
            "subheading": "Trusted inputs for healthy, high-yield fields.",
            "cta_label": "Explore",
            "cta_url": "/products",
        },
        {
            "image_url": "/images/home/hero/vegetables-banner.jpg",
            "heading": "From field to harvest",
            "subheading": "Quality you can taste, results you can measure.",
            "cta_label": "Discover",
            "cta_url": "/products",
        },
    ],
    "pl": [
        {
            "image_url": "/images/home/hero/summer-banner.jpg",
            "heading": "Sezon wzrostu już trwa",
            "subheading": "Wszystko, czego potrzebujesz na produktywny sezon.",
            "cta_label": "Zobacz produkty",
            "cta_url": "/products",
        },
        {
            "image_url": "/images/home/hero/sprayer-banner.jpg",
            "heading": "Skuteczna ochrona upraw",
            "subheading": "Sprawdzone środki dla zdrowych, wysokich plonów.",
            "cta_label": "Odkryj",
            "cta_url": "/products",
        },
        {
            "image_url": "/images/home/hero/vegetables-banner.jpg",
            "heading": "Od pola do zbiorów",
            "subheading": "Jakość, którą czuć, wyniki, które można zmierzyć.",
            "cta_label": "Sprawdź",
            "cta_url": "/products",
        },
    ],
}

_FEATURED_HEADING = {"en": "Featured products", "pl": "Polecane produkty"}
_SEO_TITLE = {"en": "Home", "pl": "Strona główna"}


def _build_layout(locale: str) -> dict[str, Any]:
    """Reproduce index.vue's section order as a validated layout blob."""
    layout = PageLayout.model_validate(
        {
            "sections": [
                # 1) promo strip (content from the Banner API at render; empty here)
                {"id": "promo-strip", "type": "promo-strip", "settings": {"text": ""}},
                # 2) hero slideshow (the three mock slides, localized)
                {
                    "id": "slideshow",
                    "type": "slideshow",
                    "settings": {
                        "autoplay": True,
                        "interval_ms": 6000,
                        "show_dots": True,
                        "show_arrows": True,
                    },
                    "blocks": [
                        {
                            "type": "slide",
                            "id": f"slide-{i}",
                            "settings": slide,
                        }
                        for i, slide in enumerate(_SLIDES[locale])
                    ],
                },
                # 3) hero banners (modelled as a collection-list; filled by Banner API)
                {
                    "id": "hero-banners",
                    "type": "collection-list",
                    "settings": {"columns": 3},
                    "blocks": [],
                },
                # 4) featured product grid
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
