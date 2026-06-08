"""Product catalog CSV import — parse, validate (dry-run), and apply.

The round-trip counterpart to ``product_export.py``. The same 31-column schema
(``product_export.CSV_HEADERS``) is the contract; this module reads it back.

Two layers, deliberately split:

1. **Pure parsing** (``parse_csv``) — decode ``utf-8-sig``, validate the header
   set, and turn each data row into a typed ``ParsedRow`` (no DB). Malformed
   cells (non-numeric price, bad bool) become per-row parse errors here.
2. **DB-aware processing** (``process_rows``) — the *single validator* shared by
   dry-run and apply. It resolves the upsert key (``id`` → ``slug`` → ``sku``),
   resolves ``category_slug`` / ``collection_slugs`` to ids, enforces the
   create-needs-name+price rule, and applies the **variant-stock rejection** (a
   variant product may not change ``stock`` via CSV — stock lives at variant
   grain). With ``apply=False`` it writes NOTHING (dry-run); with ``apply=True``
   it mutates through the route-supplied callbacks (product create/update CRUD +
   slug/SKU uniqueness + OMNIBUS price-history + ``apply_delta`` for stock).

The report shape is stable and shared with the frontend / CSV-BE3:
``{ summary: {creates, updates, skips, errors}, rows: [ReportRow...] }`` where
each ``ReportRow`` is ``{row_number, action, product_id, errors}``.

CSV cells are **data, never re-sanitized** — ``csv_safe`` lives on export only
(import does not re-prefix). Caps: ``MAX_ROWS`` rows, ``MAX_BYTES`` file size.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from app.services.product_export import CSV_HEADERS

if TYPE_CHECKING:
    from collections.abc import Sequence

# Hard caps (Risk: bound the import). 5k rows mirrors the plan; the byte cap
# keeps a pathological upload from exhausting memory before the row cap bites.
MAX_ROWS = 5000
MAX_BYTES = 8 * 1024 * 1024  # 8 MiB

Action = Literal["create", "update", "skip", "error"]

# Columns the importer maps onto product fields. ``id`` / ``has_variants`` /
# ``stock`` are handled specially (key / read-only flag / inventory path), so
# they are not in this set. ``slug`` / ``sku`` are upsert keys *and* settable.
_PRICE_FIELDS = ("price_usd", "price_pln", "price_eur")
_TEXT_FIELDS = (
    "name",
    "name_pl",
    "description",
    "description_pl",
    "slug",
    "sku",
    "brand",
    "gtin",
    "mpn",
    "meta_title",
    "meta_description",
    "meta_title_pl",
    "meta_description_pl",
)
_FLOAT_FIELDS = (*_PRICE_FIELDS, "weight_kg", "length_cm", "width_cm", "height_cm")
_INT_FIELDS = ("low_stock_threshold",)
_BOOL_FIELDS = ("allow_backorder", "is_active", "is_featured")
_CONDITIONS = ("new", "refurbished", "used")


@dataclass
class ParsedRow:
    """One CSV data row, decoded into typed cells + any parse-level errors.

    ``row_number`` is 1-based over data rows (header is not counted), matching
    what a spreadsheet user sees minus the header line. ``present`` records which
    columns were actually supplied (non-empty) so apply can do partial updates
    (omitted columns leave the existing value alone, mirroring ``ProductUpdate``
    semantics). ``values`` holds coerced python values for present cells.
    """

    row_number: int
    raw: dict[str, str]
    values: dict[str, object] = field(default_factory=dict)
    present: set[str] = field(default_factory=set)
    tags: list[str] | None = None
    collection_slugs: list[str] | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class ParsedImport:
    """Result of ``parse_csv`` — typed rows plus any whole-file errors.

    A non-empty ``header_errors`` (missing required headers / over-cap) means the
    file is unusable; the route returns a 400-shaped report with no per-row work.
    """

    rows: list[ParsedRow] = field(default_factory=list)
    header_errors: list[str] = field(default_factory=list)


def _split_packed(cell: str) -> list[str]:
    """Split a ``"; "``-packed column into a de-duped, trimmed list (order kept)."""
    out: list[str] = []
    seen: set[str] = set()
    for part in cell.split(";"):
        token = part.strip()
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def parse_csv(raw: bytes) -> ParsedImport:
    """Decode + parse the CSV into typed rows. No DB access (pure)."""
    result = ParsedImport()

    if len(raw) > MAX_BYTES:
        result.header_errors.append(f"File exceeds the {MAX_BYTES} byte limit")
        return result

    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    fieldnames = reader.fieldnames or []
    header_set = {name.strip() for name in fieldnames if name}
    # Every export header except the surrogate ``id`` is required so a hand-built
    # file stays unambiguous; unknown extra columns are ignored (forward-compat).
    required = set(CSV_HEADERS)
    missing = required - header_set
    if missing:
        result.header_errors.append("Missing required column(s): " + ", ".join(sorted(missing)))
        return result

    for index, raw_row in enumerate(reader, start=1):
        if index > MAX_ROWS:
            result.header_errors.append(f"File exceeds the {MAX_ROWS} row limit")
            return result
        result.rows.append(_parse_row(index, raw_row))

    return result


def _parse_row(row_number: int, raw_row: dict[str, str | None]) -> ParsedRow:
    cleaned = {k: (v if v is not None else "") for k, v in raw_row.items()}
    row = ParsedRow(row_number=row_number, raw=cleaned)

    def cell(name: str) -> str:
        return (cleaned.get(name) or "").strip()

    # id — surrogate upsert key; blank = none.
    id_raw = cell("id")
    if id_raw:
        try:
            row.values["id"] = int(id_raw)
            row.present.add("id")
        except ValueError:
            row.errors.append(f"id must be an integer (got {id_raw!r})")

    for name in _TEXT_FIELDS:
        if name in cleaned and cell(name) != "":
            row.values[name] = cell(name)
            row.present.add(name)

    for name in _FLOAT_FIELDS:
        if name in cleaned and cell(name) != "":
            try:
                row.values[name] = float(cell(name))
                row.present.add(name)
            except ValueError:
                row.errors.append(f"{name} must be a number (got {cell(name)!r})")

    for name in _INT_FIELDS:
        if name in cleaned and cell(name) != "":
            try:
                row.values[name] = int(cell(name))
                row.present.add(name)
            except ValueError:
                row.errors.append(f"{name} must be an integer (got {cell(name)!r})")

    for name in _BOOL_FIELDS:
        if name in cleaned and cell(name) != "":
            parsed = _parse_bool(cell(name))
            if parsed is None:
                row.errors.append(f"{name} must be true/false (got {cell(name)!r})")
            else:
                row.values[name] = parsed
                row.present.add(name)

    if cell("condition") != "":
        value = cell("condition").lower()
        if value not in _CONDITIONS:
            row.errors.append(
                f"condition must be one of {', '.join(_CONDITIONS)} (got {cell('condition')!r})"
            )
        else:
            row.values["condition"] = value
            row.present.add("condition")

    # stock — present (even blank distinguishes "set to" from "omit"); only a
    # non-empty cell is a stock *change* request.
    stock_raw = cell("stock")
    if stock_raw != "":
        try:
            row.values["stock"] = int(stock_raw)
            row.present.add("stock")
        except ValueError:
            row.errors.append(f"stock must be an integer (got {stock_raw!r})")

    # category_slug — empty cell is "no change"; an explicit value resolves later.
    if cell("category_slug") != "":
        row.values["category_slug"] = cell("category_slug")
        row.present.add("category_slug")

    # has_variants is informational on export; ignored on import (variant
    # detection comes from the DB), but parse it for the stock-reject diagnostic.
    if cell("has_variants") != "":
        parsed_hv = _parse_bool(cell("has_variants"))
        if parsed_hv is not None:
            row.values["has_variants"] = parsed_hv

    if "tags" in cleaned and cell("tags") != "":
        row.tags = _split_packed(cell("tags"))
        row.present.add("tags")
    if "collection_slugs" in cleaned and cell("collection_slugs") != "":
        row.collection_slugs = _split_packed(cell("collection_slugs"))
        row.present.add("collection_slugs")

    return row


def _parse_bool(value: str) -> bool | None:
    low = value.strip().lower()
    if low in {"true", "1", "yes", "y", "t"}:
        return True
    if low in {"false", "0", "no", "n", "f"}:
        return False
    return None


@dataclass
class ReportRow:
    """One row's outcome — the shape the FE / CSV-BE3 consume."""

    row_number: int
    action: Action
    product_id: int | None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "row_number": self.row_number,
            "action": self.action,
            "product_id": self.product_id,
            "errors": list(self.errors),
        }


def build_summary(rows: Sequence[ReportRow]) -> dict[str, int]:
    return {
        "creates": sum(1 for r in rows if r.action == "create"),
        "updates": sum(1 for r in rows if r.action == "update"),
        "skips": sum(1 for r in rows if r.action == "skip"),
        "errors": sum(1 for r in rows if r.action == "error"),
    }
