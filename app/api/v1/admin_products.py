"""Admin product catalog tools — CSV export (and, in later PRs, import + bulk edit).

Mounted at ``/api/v1/admin/products``. All routes are admin-gated. The export is a
read-only GET (no audit row, no persistence) that streams the catalog as RFC 4180
CSV (UTF-8 with BOM), inheriting the admin product-list filters (category / active
/ search) so the file matches the on-screen filtered view. One row per product;
variants are flagged via ``has_variants`` (not expanded to rows in v1). Free-text
cells pass through the formula-injection guard (``csv_safe``).
"""

from datetime import date

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_admin, get_db
from app.crud import product as crud_product
from app.models.user import User
from app.services import product_export

router = APIRouter()


def _export_filename(category_id: int | None, is_active: bool | None, search: str | None) -> str:
    """``products`` base, suffixed with active-filter / category / search markers;
    falls back to ``products-export-{today}.csv`` when nothing is filtered."""
    parts = ["products", "export"]
    if is_active is True:
        parts.append("active")
    elif is_active is False:
        parts.append("inactive")
    if category_id is not None:
        parts.append(f"cat{category_id}")
    if search:
        parts.append("search")
    if len(parts) == 2:
        parts.append(date.today().strftime("%Y%m%d"))
    return "-".join(parts) + ".csv"


@router.get(
    "/export",
    responses={200: {"content": {"text/csv": {}}, "description": "Product CSV export"}},
)
async def export_products(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    category_id: int | None = Query(None, description="Filter by category ID"),
    is_active: bool | None = Query(None, description="Filter by active status (omit = all)"),
    search: str | None = Query(None, max_length=100, description="Search in name/description"),
):
    """Download the product catalog as CSV (admin), inheriting the list filters.

    One row per product; ``tags`` / ``collection_slugs`` packed into ``"; "``-joined
    columns; variant products flagged with ``has_variants`` (not expanded — v1
    export only). RFC 4180, UTF-8 with BOM. Generated on demand — no persistence,
    no audit row (read-only GET). This is export-only; import + bulk-edit ship in
    follow-up PRs (CSV-BE2 / CSV-BE3).
    """
    products = crud_product.get_for_export(
        db, category_id=category_id, is_active=is_active, search=search
    )

    rows = [
        product_export.ProductExportRow(
            id=product.id,
            slug=product.slug,
            sku=product.sku,
            name=product.name,
            name_pl=product.name_pl,
            description=product.description,
            description_pl=product.description_pl,
            price_usd=product.price_usd,
            price_pln=product.price_pln,
            price_eur=product.price_eur,
            stock=product.stock,
            low_stock_threshold=product.low_stock_threshold,
            allow_backorder=product.allow_backorder,
            is_active=product.is_active,
            is_featured=product.is_featured,
            has_variants=bool(product.variants),
            category_slug=product.category.slug if product.category else None,
            brand=product.brand,
            gtin=product.gtin,
            mpn=product.mpn,
            condition=product.condition,
            weight_kg=product.weight_kg,
            length_cm=product.length_cm,
            width_cm=product.width_cm,
            height_cm=product.height_cm,
            meta_title=product.meta_title,
            meta_description=product.meta_description,
            meta_title_pl=product.meta_title_pl,
            meta_description_pl=product.meta_description_pl,
            tags=[tag.name for tag in product.tags],
            collection_slugs=[collection.slug for collection in product.collections],
        )
        for product in products
    ]

    csv_bytes = product_export.build_products_csv(rows)
    filename = _export_filename(category_id, is_active, search)
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
