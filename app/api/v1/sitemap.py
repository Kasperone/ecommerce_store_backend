"""Public sitemap-source endpoints (SEO blueprint D7).

Unauthenticated, like the public catalog: the consumer is the frontend's
Nitro sitemap generator (``server/api/__sitemap__/urls.ts``), which pulls
these once per sitemap-cache TTL and expands each row into per-locale URLs.

Slim by contract: slug + honest ``lastmod`` (``updated_at``), plus image
URLs for products (Google's image-sitemap entries — especially useful for
CDN-hosted images like our R2 bucket). Active rows only; deactivated
products 410 on the PDP and must not be advertised here.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.dependencies import get_db
from app.crud import category as crud_category
from app.crud import product as crud_product
from app.schemas.sitemap import SitemapCategoryEntry, SitemapProductEntry

router = APIRouter()


@router.get("/products", response_model=list[SitemapProductEntry])
async def sitemap_products(db: Session = Depends(get_db)):
    """All active products: slug, updated_at, image URLs (sort_order asc)."""
    products = crud_product.get_sitemap_entries(db)
    return [
        SitemapProductEntry(
            slug=p.slug,
            updated_at=p.updated_at,
            images=[img.url for img in p.images],
        )
        for p in products
    ]


@router.get("/categories", response_model=list[SitemapCategoryEntry])
async def sitemap_categories(db: Session = Depends(get_db)):
    """All active categories: slug, optional Polish slug, updated_at."""
    return crud_category.get_sitemap_entries(db)
