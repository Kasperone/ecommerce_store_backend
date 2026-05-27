"""Unit tests for the search endpoint helpers (``app.api.v1.search``).

These are pure functions — param → Meili-arg mapping, locale/currency
selection, hit → schema mapping — tested without an engine or HTTP client.
"""

from __future__ import annotations

from app.api.v1 import search


def test_price_field_per_currency():
    assert search._price_field("USD") == "price_usd"
    assert search._price_field("PLN") == "price_pln"
    assert search._price_field("EUR") == "price_eur"


def test_build_filters_empty_when_no_params():
    assert search.build_filters(None, None, None, None, "USD") == []


def test_build_filters_combines_all():
    filters = search.build_filters(
        category_id=3, min_price=10.0, max_price=99.0, in_stock=True, currency="PLN"
    )
    assert "category_id = 3" in filters
    assert "in_stock = true" in filters
    assert "price_pln >= 10.0" in filters
    assert "price_pln <= 99.0" in filters


def test_build_filters_in_stock_false():
    filters = search.build_filters(None, None, None, False, "USD")
    assert filters == ["in_stock = false"]


def test_build_sort_relevance_is_none():
    assert search.build_sort("relevance", "USD") is None


def test_build_sort_price_uses_currency_field():
    assert search.build_sort("price_asc", "EUR") == ["price_eur:asc"]
    assert search.build_sort("price_desc", "PLN") == ["price_pln:desc"]


def test_build_sort_name():
    assert search.build_sort("name", "USD") == ["name:asc"]


def test_localized_prefers_pl_with_fallback():
    data = {"name": "Phone", "name_pl": "Telefon"}
    assert search._localized(data, "name", "pl") == "Telefon"
    assert search._localized(data, "name", "en") == "Phone"
    # Falls back to EN when the PL field is missing/empty.
    assert search._localized({"name": "Phone"}, "name", "pl") == "Phone"


def _product_hit() -> dict:
    return {
        "id": 7,
        "slug": "phone",
        "name": "Phone",
        "name_pl": "Telefon",
        "description": "A phone",
        "description_pl": "Telefon opis",
        "sku": "SKU-7",
        "image_url": "https://cdn/p.webp",
        "in_stock": True,
        "category_id": 3,
        "category_name": "Electronics",
        "category_name_pl": "Elektronika",
        "price_usd": 99.0,
        "price_pln": 399.0,
        "price_eur": 89.0,
        "_formatted": {"name": "<em>Pho</em>ne", "name_pl": "<em>Tele</em>fon"},
    }


def test_map_product_hit_localizes_en():
    item = search.map_product_hit(_product_hit(), "en")
    assert item.name == "Phone"
    assert item.description == "A phone"
    assert item.category_name == "Electronics"
    assert item.name_highlight == "<em>Pho</em>ne"
    # All three prices pass through; the frontend selects + formats.
    assert item.price_usd == 99.0
    assert item.price_pln == 399.0
    assert item.in_stock is True


def test_map_product_hit_localizes_pl():
    item = search.map_product_hit(_product_hit(), "pl")
    assert item.name == "Telefon"
    assert item.description == "Telefon opis"
    assert item.category_name == "Elektronika"
    assert item.name_highlight == "<em>Tele</em>fon"


def test_map_product_hit_highlight_falls_back_to_plain_name():
    hit = _product_hit()
    del hit["_formatted"]
    item = search.map_product_hit(hit, "en")
    assert item.name_highlight == "Phone"


def test_map_suggest_product_and_category():
    product = search.map_suggest_product(_product_hit(), "pl")
    assert product.name == "Telefon"
    assert product.name_highlight == "<em>Tele</em>fon"
    assert product.price_eur == 89.0

    category = search.map_suggest_category(
        {"id": 3, "slug": "electronics", "name": "Electronics", "name_pl": "Elektronika"}, "pl"
    )
    assert category.name == "Elektronika"
    assert category.slug == "electronics"
