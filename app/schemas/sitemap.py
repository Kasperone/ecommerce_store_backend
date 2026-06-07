"""Slim public sitemap-source schemas (SEO blueprint D7).

Deliberately minimal: the frontend's sitemap generator only needs the slug,
an honest ``lastmod`` (``updated_at``), and — for products — the image URLs
for ``<image:image>`` entries. Everything else stays out of the payload so
the endpoint stays cheap to serve and to cache.
"""

from datetime import datetime

from app.schemas.base import BaseSchema


class SitemapProductEntry(BaseSchema):
    """One active product: slug + lastmod + image URLs (sorted by sort_order)."""

    slug: str
    updated_at: datetime
    images: list[str]


class SitemapCategoryEntry(BaseSchema):
    """One active category: slug (+ optional Polish slug) + lastmod."""

    slug: str
    slug_pl: str | None
    updated_at: datetime
