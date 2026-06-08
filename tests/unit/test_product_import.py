"""Unit tests for the pure CSV import parser (``app.services.product_import``).

No DB. Covers header validation, packed tags/collection splitting, typed-cell
coercion + per-cell parse errors, the row/byte caps, and that an injected
formula cell (``=cmd``) is parsed as plain data (csv_safe is export-only).
"""

from __future__ import annotations

import csv
import io

from app.services import product_import
from app.services.product_export import CSV_HEADERS


def _csv(rows: list[dict[str, str]], headers: list[str] | None = None) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers or CSV_HEADERS)
    writer.writeheader()
    for row in rows:
        writer.writerow({h: row.get(h, "") for h in (headers or CSV_HEADERS)})
    return buf.getvalue().encode("utf-8-sig")


def test_parse_rejects_missing_headers():
    raw = b"slug,name\nfoo,Foo\n"
    parsed = product_import.parse_csv(raw)
    assert parsed.header_errors
    assert "Missing required column" in parsed.header_errors[0]
    assert parsed.rows == []


def test_parse_accepts_full_header_set():
    raw = _csv([{"slug": "foo", "name": "Foo", "price_usd": "9.99"}])
    parsed = product_import.parse_csv(raw)
    assert parsed.header_errors == []
    assert len(parsed.rows) == 1
    row = parsed.rows[0]
    assert row.values["slug"] == "foo"
    assert row.values["name"] == "Foo"
    assert row.values["price_usd"] == 9.99
    assert "name" in row.present


def test_parse_utf8_sig_tolerant_and_diacritics():
    raw = _csv([{"slug": "pl", "name": "Świeży", "name_pl": "Świeży produkt"}])
    parsed = product_import.parse_csv(raw)
    assert parsed.rows[0].values["name"] == "Świeży"
    assert parsed.rows[0].values["name_pl"] == "Świeży produkt"


def test_parse_splits_packed_tags_and_collections():
    raw = _csv(
        [{"slug": "p", "tags": "sale; new-arrival; sale", "collection_slugs": "summer; sale"}]
    )
    row = product_import.parse_csv(raw).rows[0]
    # De-duped, trimmed, order preserved.
    assert row.tags == ["sale", "new-arrival"]
    assert row.collection_slugs == ["summer", "sale"]
    assert "tags" in row.present


def test_parse_empty_packed_columns_are_none():
    raw = _csv([{"slug": "p", "tags": "", "collection_slugs": ""}])
    row = product_import.parse_csv(raw).rows[0]
    assert row.tags is None
    assert row.collection_slugs is None


def test_parse_bool_and_numeric_coercion():
    raw = _csv(
        [
            {
                "slug": "p",
                "is_active": "true",
                "is_featured": "false",
                "allow_backorder": "1",
                "low_stock_threshold": "5",
                "weight_kg": "1.5",
            }
        ]
    )
    row = product_import.parse_csv(raw).rows[0]
    assert row.values["is_active"] is True
    assert row.values["is_featured"] is False
    assert row.values["allow_backorder"] is True
    assert row.values["low_stock_threshold"] == 5
    assert row.values["weight_kg"] == 1.5


def test_parse_bad_numeric_is_row_error():
    raw = _csv([{"slug": "p", "price_usd": "abc"}])
    row = product_import.parse_csv(raw).rows[0]
    assert any("price_usd must be a number" in e for e in row.errors)


def test_parse_bad_condition_is_row_error():
    raw = _csv([{"slug": "p", "condition": "antique"}])
    row = product_import.parse_csv(raw).rows[0]
    assert any("condition must be one of" in e for e in row.errors)


def test_injected_formula_cell_is_plain_data():
    # csv_safe lives on export only — on import the cell is data, not re-prefixed.
    raw = _csv([{"slug": "p", "name": "=cmd|'/c calc'!A0"}])
    row = product_import.parse_csv(raw).rows[0]
    name = row.values["name"]
    assert name == "=cmd|'/c calc'!A0"
    assert isinstance(name, str) and name.startswith("=")  # untouched, no leading quote added


def test_parse_row_cap_enforced():
    rows = [{"slug": f"p{i}", "name": f"P{i}"} for i in range(product_import.MAX_ROWS + 1)]
    parsed = product_import.parse_csv(_csv(rows))
    assert any("row limit" in e for e in parsed.header_errors)


def test_parse_byte_cap_enforced():
    raw = b"x" * (product_import.MAX_BYTES + 1)
    parsed = product_import.parse_csv(raw)
    assert any("byte limit" in e for e in parsed.header_errors)


def test_build_summary_counts():
    rows = [
        product_import.ReportRow(1, "create", None),
        product_import.ReportRow(2, "update", 5),
        product_import.ReportRow(3, "skip", 6),
        product_import.ReportRow(4, "error", None, ["boom"]),
    ]
    summary = product_import.build_summary(rows)
    assert summary == {"creates": 1, "updates": 1, "skips": 1, "errors": 1}
