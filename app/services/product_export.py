"""Product catalog CSV export (RFC 4180; UTF-8 with BOM for Excel + Polish diacritics).

Pure: ``build_products_csv`` turns an in-memory sequence of ``ProductExportRow``
into CSV bytes. No DB access and no Settings dependency, so it is unit-testable in
isolation like ``inventory_export.py`` / ``order_export.py``. The route assembles
the rows (eager-loading tags/collections/category/variants) and calls this.

Shape: **one row per product** — the catalog view. Per-product ``tags`` and
``collection_slugs`` are each packed into a single ``"; "``-joined column (the
packed-column convention shared with the inventory export). Variants are NOT
expanded to rows in v1 — the row carries a ``has_variants`` boolean flag only
(variant CSV round-tripping is deferred to a later CSV). Stable English
snake_case headers; free-text cells go through ``csv_safe`` (formula-injection
guard); machine numbers/bools/ids are rendered raw.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.services._csv_safe import csv_safe

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class ProductExportRow:
    """One product's catalog snapshot for the CSV."""

    id: int
    slug: str
    sku: str | None
    name: str
    name_pl: str | None
    description: str | None
    description_pl: str | None
    price_usd: float | None
    price_pln: float | None
    price_eur: float | None
    stock: int
    low_stock_threshold: int | None
    allow_backorder: bool
    is_active: bool
    is_featured: bool
    has_variants: bool
    category_slug: str | None
    brand: str | None
    gtin: str | None
    mpn: str | None
    condition: str | None
    weight_kg: float | None
    length_cm: float | None
    width_cm: float | None
    height_cm: float | None
    meta_title: str | None
    meta_description: str | None
    meta_title_pl: str | None
    meta_description_pl: str | None
    tags: list[str] = field(default_factory=list)
    collection_slugs: list[str] = field(default_factory=list)


CSV_HEADERS: list[str] = [
    "id",
    "slug",
    "sku",
    "name",
    "name_pl",
    "description",
    "description_pl",
    "price_usd",
    "price_pln",
    "price_eur",
    "stock",
    "low_stock_threshold",
    "allow_backorder",
    "is_active",
    "is_featured",
    "has_variants",
    "category_slug",
    "brand",
    "gtin",
    "mpn",
    "condition",
    "weight_kg",
    "length_cm",
    "width_cm",
    "height_cm",
    "meta_title",
    "meta_description",
    "meta_title_pl",
    "meta_description_pl",
    "tags",
    "collection_slugs",
]


def _num(value: float | int | None) -> str:
    """Render a nullable numeric cell — empty string for NULL, else server-derived."""
    return "" if value is None else str(value)


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _row(row: ProductExportRow) -> list[str]:
    # Catalog-controlled free-text cells (name(_pl)/description(_pl)/meta_*/
    # brand/gtin/mpn/category & collection slugs/tags) go through ``csv_safe``;
    # numeric/bool/id cells are server-derived and rendered raw.
    return [
        str(row.id),
        csv_safe(row.slug),
        csv_safe(row.sku or ""),
        csv_safe(row.name),
        csv_safe(row.name_pl or ""),
        csv_safe(row.description or ""),
        csv_safe(row.description_pl or ""),
        _num(row.price_usd),
        _num(row.price_pln),
        _num(row.price_eur),
        str(row.stock),
        _num(row.low_stock_threshold),
        _bool(row.allow_backorder),
        _bool(row.is_active),
        _bool(row.is_featured),
        _bool(row.has_variants),
        csv_safe(row.category_slug or ""),
        csv_safe(row.brand or ""),
        csv_safe(row.gtin or ""),
        csv_safe(row.mpn or ""),
        csv_safe(row.condition or ""),
        _num(row.weight_kg),
        _num(row.length_cm),
        _num(row.width_cm),
        _num(row.height_cm),
        csv_safe(row.meta_title or ""),
        csv_safe(row.meta_description or ""),
        csv_safe(row.meta_title_pl or ""),
        csv_safe(row.meta_description_pl or ""),
        csv_safe("; ".join(row.tags)),
        csv_safe("; ".join(row.collection_slugs)),
    ]


def build_products_csv(rows: Sequence[ProductExportRow]) -> bytes:
    """Render product rows to RFC 4180 CSV bytes (UTF-8 with BOM). Header-only if empty."""
    buf = io.StringIO()
    writer = csv.writer(buf)  # excel dialect: comma, \r\n, QUOTE_MINIMAL
    writer.writerow(CSV_HEADERS)
    for row in rows:
        writer.writerow(_row(row))
    return buf.getvalue().encode("utf-8-sig")
