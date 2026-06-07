"""Unit tests for the best-effort Meilisearch sync layer (``app.services.search_index``).

Two concerns:

1. **Document builders** are pure functions over a loaded ORM object — tested
   directly against factory-built rows (DB-backed).
2. **Sync gating** — every ``sync_*`` must no-op when Meilisearch is
   unconfigured (this is what keeps the rest of the suite green without an
   engine), and otherwise upsert active / delete gone-or-inactive via a
   **mocked** client (no live engine needed in CI).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.models.product_image import ProductImage
from app.services import search_index


@pytest.fixture
def mock_meili(monkeypatch):
    """Pretend Meili is configured; route build_client() to a mock client."""
    client = MagicMock()
    monkeypatch.setattr(search_index, "meili_configured", lambda: True)
    monkeypatch.setattr(search_index, "build_client", lambda: client)
    return client


def _add_image(db_session, product_id, **overrides):
    defaults = {
        "product_id": product_id,
        "url": "https://cdn.example.com/img.webp",
        "alt_text": None,
        "sort_order": 0,
        "is_primary": False,
        "width": 800,
        "height": 600,
        "bytes": 1234,
        "mime": "image/webp",
    }
    defaults.update(overrides)
    image = ProductImage(**defaults)
    db_session.add(image)
    return image


# ---------------------------------------------------------------------------
# Document builders
# ---------------------------------------------------------------------------


def test_build_product_document_full(db_session, product_factory):
    product = product_factory(name="Phone", name_pl="Telefon", stock=3, is_featured=True)
    _add_image(db_session, product.id, url="https://cdn/a.webp", sort_order=0, is_primary=False)
    _add_image(
        db_session, product.id, url="https://cdn/primary.webp", sort_order=1, is_primary=True
    )
    db_session.commit()
    db_session.refresh(product)

    doc = search_index.build_product_document(product)

    assert doc["id"] == product.id
    assert doc["name"] == "Phone"
    assert doc["name_pl"] == "Telefon"
    assert doc["in_stock"] is True
    assert doc["is_featured"] is True
    # Primary image wins over the lower sort_order non-primary one.
    assert doc["image_url"] == "https://cdn/primary.webp"
    assert doc["category_name"] == product.category.name
    assert doc["category_id"] == product.category_id
    assert isinstance(doc["created_at"], int)


def test_build_product_document_falls_back_to_first_image_when_no_primary(
    db_session, product_factory
):
    product = product_factory()
    _add_image(db_session, product.id, url="https://cdn/first.webp", sort_order=0)
    _add_image(db_session, product.id, url="https://cdn/second.webp", sort_order=1)
    db_session.commit()
    db_session.refresh(product)

    doc = search_index.build_product_document(product)
    assert doc["image_url"] == "https://cdn/first.webp"


def test_build_product_document_minimal(db_session, product_factory):
    product = product_factory(category=None, stock=0)

    doc = search_index.build_product_document(product)

    assert doc["category_name"] is None
    assert doc["category_name_pl"] is None
    assert doc["image_url"] is None
    assert doc["in_stock"] is False
    # Variantless product: prices are the product columns, no option values.
    assert doc["has_variants"] is False
    assert doc["option_values"] == []
    assert doc["price_usd"] == product.price_usd


def _add_variant(db_session, product, *, value, value_pl=None, **overrides):
    """Materialise a one-option variant row directly (no matrix machinery)."""
    from app.models.product_variant import ProductOption, ProductOptionValue, ProductVariant

    option = next((o for o in product.options if o.name == "Color"), None)
    if option is None:
        option = ProductOption(name="Color", position=0)
        product.options.append(option)
    option_value = ProductOptionValue(value=value, value_pl=value_pl, position=len(option.values))
    option.values.append(option_value)
    db_session.flush()
    defaults = {
        "product_id": product.id,
        "title": value,
        "option_signature": str(option_value.id),
        "stock": 0,
        "is_active": True,
    }
    defaults.update(overrides)
    variant = ProductVariant(**defaults)
    variant.option_values = [option_value]
    db_session.add(variant)
    db_session.flush()
    return variant


def test_build_product_document_variant_min_price_and_option_values(db_session, product_factory):
    """Variant product: prices become the min across active variants
    (inherit-aware), has_variants flips, option values flatten EN+PL."""
    product = product_factory(stock=0, price_usd=20.0, price_pln=80.0, price_eur=18.0)
    _add_variant(db_session, product, value="Red", value_pl="Czerwony", price_usd=15.0)
    # Inherits USD (20.0) — min stays 15.0; PLN override 70.0 becomes the min.
    _add_variant(db_session, product, value="Blue", value_pl="Niebieski", price_pln=70.0)
    # Inactive variants don't contribute to price or visibility.
    _add_variant(db_session, product, value="Green", price_usd=1.0, is_active=False)
    db_session.commit()
    db_session.refresh(product)

    doc = search_index.build_product_document(product)

    assert doc["has_variants"] is True
    assert doc["price_usd"] == 15.0  # min(15.0 override, 20.0 inherited)
    assert doc["price_pln"] == 70.0  # min(80.0 inherited, 70.0 override)
    assert doc["price_eur"] == 18.0  # both inherit
    # All defined values flatten (EN + PL where present), inactive variants'
    # values included — they are catalog vocabulary, not offers.
    assert doc["option_values"] == [
        "Red",
        "Czerwony",
        "Blue",
        "Niebieski",
        "Green",
    ]


def test_build_category_document(db_session, category_factory):
    parent = category_factory(name="Electronics")
    child = category_factory(name="Phones", name_pl="Telefony", parent_id=parent.id)
    db_session.refresh(child)

    doc = search_index.build_category_document(child, product_count=7)

    assert doc["id"] == child.id
    assert doc["name"] == "Phones"
    assert doc["name_pl"] == "Telefony"
    assert doc["parent_name"] == "Electronics"
    assert doc["product_count"] == 7


# ---------------------------------------------------------------------------
# Sync gating — the no-op-when-unconfigured contract
# ---------------------------------------------------------------------------


def test_sync_product_noop_when_unconfigured(db_session, product_factory, monkeypatch):
    product = product_factory()
    build_client_mock = MagicMock()
    monkeypatch.setattr(search_index, "meili_configured", lambda: False)
    monkeypatch.setattr(search_index, "build_client", build_client_mock)

    search_index.sync_product(db_session, product.id)

    # Returns before ever touching the client — proves the suite stays green
    # without a running engine.
    build_client_mock.assert_not_called()


def test_delete_product_noop_when_unconfigured(monkeypatch):
    build_client_mock = MagicMock()
    monkeypatch.setattr(search_index, "meili_configured", lambda: False)
    monkeypatch.setattr(search_index, "build_client", build_client_mock)

    search_index.delete_product(123)

    build_client_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Sync operations against a mocked client
# ---------------------------------------------------------------------------


def test_sync_product_upserts_active(db_session, product_factory, mock_meili):
    product = product_factory(stock=5, is_active=True)

    search_index.sync_product(db_session, product.id)

    mock_meili.index.assert_called_with(search_index.PRODUCTS_INDEX)
    index = mock_meili.index.return_value
    index.add_documents.assert_called_once()
    docs, kwargs = index.add_documents.call_args.args[0], index.add_documents.call_args.kwargs
    assert docs[0]["id"] == product.id
    assert kwargs["primary_key"] == "id"
    index.delete_document.assert_not_called()


def test_sync_product_deletes_when_inactive(db_session, product_factory, mock_meili):
    product = product_factory(is_active=False)

    search_index.sync_product(db_session, product.id)

    index = mock_meili.index.return_value
    index.delete_document.assert_called_once_with(str(product.id))
    index.add_documents.assert_not_called()


def test_sync_product_deletes_when_missing(db_session, mock_meili):
    search_index.sync_product(db_session, 999_999)

    index = mock_meili.index.return_value
    index.delete_document.assert_called_once_with("999999")


def test_sync_product_swallows_engine_errors(db_session, product_factory, mock_meili):
    product = product_factory()
    mock_meili.index.return_value.add_documents.side_effect = RuntimeError("engine down")

    # Best-effort: a thrown engine error must not propagate to the caller.
    search_index.sync_product(db_session, product.id)


def test_delete_product_calls_delete_document(mock_meili):
    search_index.delete_product(42)

    mock_meili.index.assert_called_with(search_index.PRODUCTS_INDEX)
    mock_meili.index.return_value.delete_document.assert_called_once_with("42")


def test_sync_category_counts_active_products(
    db_session, category_factory, product_factory, mock_meili
):
    category = category_factory(is_active=True)
    product_factory(category=category, is_active=True)
    product_factory(category=category, is_active=False)  # excluded from the count

    search_index.sync_category(db_session, category.id)

    mock_meili.index.assert_called_with(search_index.CATEGORIES_INDEX)
    doc = mock_meili.index.return_value.add_documents.call_args.args[0][0]
    assert doc["id"] == category.id
    assert doc["product_count"] == 1


def test_sync_products_bulk_mixes_upsert_and_delete(db_session, product_factory, mock_meili):
    active = product_factory(is_active=True)
    inactive = product_factory(is_active=False)

    search_index.sync_products_bulk(db_session, [active.id, inactive.id, 999_999])

    index = mock_meili.index.return_value
    upserted = index.add_documents.call_args.args[0]
    assert [d["id"] for d in upserted] == [active.id]
    deleted = index.delete_documents.call_args.args[0]
    assert set(deleted) == {str(inactive.id), "999999"}


# ---------------------------------------------------------------------------
# Index configuration
# ---------------------------------------------------------------------------


def test_configure_indexes_creates_both_with_primary_key(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(search_index, "build_client", lambda: client)

    search_index.configure_indexes()

    client.create_index.assert_any_call(search_index.PRODUCTS_INDEX, {"primaryKey": "id"})
    client.create_index.assert_any_call(search_index.CATEGORIES_INDEX, {"primaryKey": "id"})
    # Settings applied to both indexes.
    assert client.index.return_value.update_settings.call_count == 2


def test_configure_indexes_tolerates_existing_index(monkeypatch):
    from meilisearch.errors import MeilisearchApiError

    client = MagicMock()
    already = MeilisearchApiError.__new__(MeilisearchApiError)
    already.code = "index_already_exists"
    already.message = "Index already exists"
    client.create_index.side_effect = already
    monkeypatch.setattr(search_index, "build_client", lambda: client)

    # Must not raise — re-running configure on existing indexes is expected.
    search_index.configure_indexes()
    assert client.index.return_value.update_settings.call_count == 2
