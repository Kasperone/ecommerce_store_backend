"""Seed the ``home`` CMS page (the storefront homepage renders it via PageRenderer).

    python -m app.scripts.seed_homepage_page

Idempotent (get-or-create on slug+locale): creates one published ``home`` page per
locale (``en`` + ``pl``) with this section order:

    promo-strip  →  slideshow  →  hero-banners (collection-list)  →  product-grid (featured)

The slideshow seeds three localized hero slides whose images live in R2 under
``cms/home/hero/`` (the URL is built from ``R2_PUBLIC_URL`` so the seed is
portable across buckets); promo-strip + hero-banners are empty CMS blocks (they
render nothing until an admin adds content). On rerun, existing pages are LEFT
UNTOUCHED so an admin's edits survive — only a missing locale row is inserted.

The hero images are **uploaded-if-missing** on seed: the bytes are bundled under
``app/scripts/assets/home/hero/`` and pushed to R2 only when the object isn't
already there (best-effort; skipped entirely when R2 isn't configured). So a
first seed on a fresh bucket is self-contained. Exits 0 on success; non-zero on
error.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.storage import build_public_url, get_r2_client
from app.crud import page as crud_page
from app.models.page import PageStatus, PageType
from app.schemas.page import PageLayout
from app.services.image import upload_to_r2

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("seed_homepage_page")


# Hero slide images: bundled bytes (``app/scripts/assets/home/hero/``) uploaded
# to R2 under ``cms/home/hero/`` if missing; the public URL is resolved from
# ``R2_PUBLIC_URL`` so the same keys work in any R2-configured environment.
_HERO_ASSET_DIR = Path(__file__).parent / "assets" / "home" / "hero"
_HERO_FILENAMES = {
    "summer": "summer-banner.jpg",
    "sprayer": "sprayer-banner.jpg",
    "vegetables": "vegetables-banner.jpg",
}


def _hero_key(filename: str) -> str:
    return f"cms/home/hero/{filename}"


_HERO_IMAGE = {name: build_public_url(_hero_key(fn)) for name, fn in _HERO_FILENAMES.items()}


# Localized hero slides.
_SLIDES: dict[str, list[dict[str, Any]]] = {
    "en": [
        {
            "image_url": _HERO_IMAGE["summer"],
            "heading": "The growing season is here",
            "subheading": "Everything you need for a productive season.",
            "cta_label": "Shop products",
            "cta_url": "/products",
        },
        {
            "image_url": _HERO_IMAGE["sprayer"],
            "heading": "Crop protection that performs",
            "subheading": "Trusted inputs for healthy, high-yield fields.",
            "cta_label": "Explore",
            "cta_url": "/products",
        },
        {
            "image_url": _HERO_IMAGE["vegetables"],
            "heading": "From field to harvest",
            "subheading": "Quality you can taste, results you can measure.",
            "cta_label": "Discover",
            "cta_url": "/products",
        },
    ],
    "pl": [
        {
            "image_url": _HERO_IMAGE["summer"],
            "heading": "Sezon wzrostu już trwa",
            "subheading": "Wszystko, czego potrzebujesz na produktywny sezon.",
            "cta_label": "Zobacz produkty",
            "cta_url": "/products",
        },
        {
            "image_url": _HERO_IMAGE["sprayer"],
            "heading": "Skuteczna ochrona upraw",
            "subheading": "Sprawdzone środki dla zdrowych, wysokich plonów.",
            "cta_label": "Odkryj",
            "cta_url": "/products",
        },
        {
            "image_url": _HERO_IMAGE["vegetables"],
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
    """Build the homepage section order as a validated layout blob."""
    layout = PageLayout.model_validate(
        {
            "sections": [
                # 1) promo strip (empty — an admin can add a promo message)
                {"id": "promo-strip", "type": "promo-strip", "settings": {"text": ""}},
                # 2) hero slideshow (three localized slides, images in R2)
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
                # 3) hero banners (empty collection-list — an admin can add tiles)
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


def _ensure_hero_images() -> None:
    """Upload each bundled hero image to R2 if the object is missing.

    Best-effort and idempotent: an object that already exists is left untouched,
    so reruns don't re-upload. Skipped silently when R2 isn't configured — the
    page is still seeded with the URLs (they just won't resolve until R2 is set
    up and the images land)."""
    try:
        client = get_r2_client()
    except HTTPException:
        logger.info("R2 not configured — skipping hero image upload")
        return

    bucket = settings.R2_BUCKET_NAME
    for filename in _HERO_FILENAMES.values():
        key = _hero_key(filename)
        try:
            client.head_object(Bucket=bucket, Key=key)
            logger.info("hero image already in R2: %s", key)
            continue
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("404", "NoSuchKey", "NotFound"):
                raise  # a real error (auth/network) — don't mask it

        body = (_HERO_ASSET_DIR / filename).read_bytes()
        upload_to_r2(client, bucket=bucket, key=key, body=body, content_type="image/jpeg")
        logger.info("uploaded hero image to R2: %s (%d bytes)", key, len(body))


def main() -> int:
    db = SessionLocal()
    try:
        _ensure_hero_images()
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
