"""Unit tests for the product catalog CSV export builder (pure, no DB)."""

from __future__ import annotations

import csv
import io

from app.services.product_export import CSV_HEADERS, ProductExportRow, build_products_csv


def _parse(data: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def _row(**overrides) -> ProductExportRow:
    base = dict(
        id=1,
        slug="widget",
        sku="WD-1",
        name="Widget",
        name_pl="Widżet",
        description="A widget",
        description_pl="Widżet opis",
        price_usd=19.99,
        price_pln=79.99,
        price_eur=17.99,
        stock=10,
        low_stock_threshold=3,
        allow_backorder=False,
        is_active=True,
        is_featured=False,
        has_variants=False,
        category_slug="gadgets",
        brand="Acme",
        gtin="01234567890123",
        mpn="MPN-1",
        condition="new",
        weight_kg=1.5,
        length_cm=10.0,
        width_cm=5.0,
        height_cm=2.0,
        meta_title="Widget — buy",
        meta_description="Best widget",
        meta_title_pl="Widżet — kup",
        meta_description_pl="Najlepszy widżet",
        tags=["sale", "new-arrival"],
        collection_slugs=["summer", "featured"],
    )
    base.update(overrides)
    return ProductExportRow(**base)  # type: ignore[arg-type]


def test_empty_is_header_only_with_bom():
    data = build_products_csv([])
    assert data.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM (Excel + PL diacritics)
    assert _parse(data) == []
    assert data.decode("utf-8-sig").splitlines()[0].split(",") == CSV_HEADERS


def test_header_order_is_stable_and_exact():
    assert CSV_HEADERS == [
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


def test_row_renders_all_fields():
    r = _parse(build_products_csv([_row()]))[0]
    assert r["id"] == "1"
    assert r["slug"] == "widget"
    assert r["sku"] == "WD-1"
    assert r["name"] == "Widget"
    assert r["name_pl"] == "Widżet"
    assert r["price_usd"] == "19.99"
    assert r["price_pln"] == "79.99"
    assert r["price_eur"] == "17.99"
    assert r["stock"] == "10"
    assert r["low_stock_threshold"] == "3"
    assert r["allow_backorder"] == "false"
    assert r["is_active"] == "true"
    assert r["is_featured"] == "false"
    assert r["has_variants"] == "false"
    assert r["category_slug"] == "gadgets"
    assert r["brand"] == "Acme"
    assert r["gtin"] == "01234567890123"
    assert r["mpn"] == "MPN-1"
    assert r["condition"] == "new"
    assert r["weight_kg"] == "1.5"
    assert r["length_cm"] == "10.0"
    assert r["meta_title"] == "Widget — buy"
    assert r["meta_description_pl"] == "Najlepszy widżet"


def test_tags_and_collections_packed_with_semicolon():
    r = _parse(build_products_csv([_row()]))[0]
    assert r["tags"] == "sale; new-arrival"
    assert r["collection_slugs"] == "summer; featured"


def test_empty_tags_and_collections_render_empty():
    r = _parse(build_products_csv([_row(tags=[], collection_slugs=[])]))[0]
    assert r["tags"] == ""
    assert r["collection_slugs"] == ""


def test_has_variants_true():
    r = _parse(build_products_csv([_row(has_variants=True)]))[0]
    assert r["has_variants"] == "true"


def test_nullable_cells_render_empty():
    r = _parse(
        build_products_csv(
            [
                _row(
                    sku=None,
                    name_pl=None,
                    description=None,
                    description_pl=None,
                    price_usd=None,
                    price_pln=None,
                    price_eur=29.99,
                    low_stock_threshold=None,
                    category_slug=None,
                    brand=None,
                    gtin=None,
                    mpn=None,
                    condition=None,
                    weight_kg=None,
                    length_cm=None,
                    width_cm=None,
                    height_cm=None,
                    meta_title=None,
                    meta_description=None,
                    meta_title_pl=None,
                    meta_description_pl=None,
                )
            ]
        )
    )[0]
    assert r["sku"] == ""
    assert r["name_pl"] == ""
    assert r["description"] == ""
    assert r["price_usd"] == ""
    assert r["price_eur"] == "29.99"
    assert r["low_stock_threshold"] == ""
    assert r["category_slug"] == ""
    assert r["brand"] == ""
    assert r["weight_kg"] == ""
    assert r["meta_title"] == ""


def test_formula_injection_in_free_text_is_neutralized():
    # Free-text cells starting with a formula trigger must be quoted (CWE-1236).
    r = _parse(
        build_products_csv(
            [
                _row(
                    name="=cmd|'/c calc'!A1",
                    brand="@evil",
                    tags=["=HYPERLINK(1)", "ok"],
                    meta_title="-2+3",
                )
            ]
        )
    )[0]
    assert r["name"] == "'=cmd|'/c calc'!A1"
    assert r["brand"] == "'@evil"
    # The whole packed tags cell leads with '=' → guarded as one cell.
    assert r["tags"] == "'=HYPERLINK(1); ok"
    assert r["meta_title"] == "'-2+3"


def test_rfc4180_quoting_of_commas_quotes_newlines():
    raw = build_products_csv([_row(name='Wid, "get"\nline2', description="a,b,c")]).decode(
        "utf-8-sig"
    )
    # The name cell contains a comma, a quote, and a newline → must be quoted and
    # internal quotes doubled, per RFC 4180; round-trips back to the original.
    r = _parse(build_products_csv([_row(name='Wid, "get"\nline2', description="a,b,c")]))[0]
    assert r["name"] == 'Wid, "get"\nline2'
    assert r["description"] == "a,b,c"
    assert '"Wid, ""get""' in raw
