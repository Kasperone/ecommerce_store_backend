"""Unit tests for the inventory CSV export builder (pure, no DB)."""

from __future__ import annotations

import csv
import io

from app.services.inventory_export import CSV_HEADERS, InventoryExportRow, build_inventory_csv


def _parse(data: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def test_empty_is_header_only_with_bom():
    data = build_inventory_csv([])
    assert data.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM (Excel + PL diacritics)
    assert _parse(data) == []
    assert data.decode("utf-8-sig").splitlines()[0].split(",") == CSV_HEADERS


def test_row_packs_warehouses_and_fields():
    row = InventoryExportRow(
        product_id=7,
        name="Widget",
        sku="WD-1",
        stock=10,
        low_stock_threshold=3,
        status="in_stock",
        allow_backorder=False,
        warehouses=[("MAIN", 6), ("EU-1", 4)],
    )
    r = _parse(build_inventory_csv([row]))[0]
    assert r["product_id"] == "7"
    assert r["name"] == "Widget"
    assert r["sku"] == "WD-1"
    assert r["stock"] == "10"
    assert r["low_stock_threshold"] == "3"
    assert r["status"] == "in_stock"
    assert r["allow_backorder"] == "false"
    assert r["warehouses"] == "MAIN: 6; EU-1: 4"


def test_formula_injection_in_name_is_neutralized():
    # A product name starting with '=' must be quoted (SEC-008 / CWE-1236).
    row = InventoryExportRow(
        product_id=1,
        name="=HYPERLINK(1)",
        sku="=SKU",
        stock=1,
        low_stock_threshold=None,
        status="in_stock",
        allow_backorder=False,
        warehouses=[("MAIN", 1)],
    )
    r = _parse(build_inventory_csv([row]))[0]
    assert r["name"] == "'=HYPERLINK(1)"
    assert r["sku"] == "'=SKU"


def test_none_sku_and_threshold_render_empty():
    row = InventoryExportRow(
        product_id=1,
        name="No SKU",
        sku=None,
        stock=0,
        low_stock_threshold=None,
        status="out_of_stock",
        allow_backorder=False,
        warehouses=[("MAIN", 0)],
    )
    r = _parse(build_inventory_csv([row]))[0]
    assert r["sku"] == ""
    assert r["low_stock_threshold"] == ""
    assert r["warehouses"] == "MAIN: 0"
