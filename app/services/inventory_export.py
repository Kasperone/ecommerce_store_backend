"""Inventory CSV export (RFC 4180; UTF-8 with BOM for Excel + Polish diacritics).

Pure: ``build_inventory_csv`` turns an in-memory sequence of ``InventoryExportRow``
into CSV bytes. No DB access and no Settings dependency, so it is unit-testable in
isolation like ``order_export.py`` / ``invoice.py``. The route assembles the rows
(product totals + per-warehouse breakdown) and calls this.

Shape: **one row per product** — the bookkeeping view. The per-warehouse levels
are packed into a single ``warehouses`` column (``"MAIN: 6; EU-1: 4"``) so the
file stays flat; the ``stock`` column is the authoritative total (which the packed
levels sum to). Stable English snake_case headers, raw status value, machine
numbers — same conventions as the order export.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class InventoryExportRow:
    """One product's inventory snapshot for the CSV (warehouses default-first)."""

    product_id: int
    name: str
    sku: str | None
    stock: int
    low_stock_threshold: int | None
    status: str
    warehouses: list[tuple[str, int]]  # (warehouse_code, quantity)


CSV_HEADERS: list[str] = [
    "product_id",
    "name",
    "sku",
    "stock",
    "low_stock_threshold",
    "status",
    "warehouses",
]


def _pack_warehouses(row: InventoryExportRow) -> str:
    """``"MAIN: 6; EU-1: 4"`` — code: qty per warehouse, default first (caller-sorted)."""
    return "; ".join(f"{code}: {qty}" for code, qty in row.warehouses)


def _row(row: InventoryExportRow) -> list[str]:
    return [
        str(row.product_id),
        row.name,
        row.sku or "",
        str(row.stock),
        "" if row.low_stock_threshold is None else str(row.low_stock_threshold),
        row.status,
        _pack_warehouses(row),
    ]


def build_inventory_csv(rows: Sequence[InventoryExportRow]) -> bytes:
    """Render inventory rows to RFC 4180 CSV bytes (UTF-8 with BOM). Header-only if empty."""
    buf = io.StringIO()
    writer = csv.writer(buf)  # excel dialect: comma, \r\n, QUOTE_MINIMAL
    writer.writerow(CSV_HEADERS)
    for row in rows:
        writer.writerow(_row(row))
    return buf.getvalue().encode("utf-8-sig")
