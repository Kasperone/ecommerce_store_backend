"""Google Shopping product-feed endpoint tests (Pillar E).

Covers the public ``GET /api/v1/feeds/google/{country}.xml`` feed: valid XML,
the ``g:`` namespace, item selection (active + priced-in-currency), currency
filtering, availability mapping, the locked id scheme, variant items sharing
``item_group_id``, and PL language fallback.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from app.models.product_image import ProductImage
from app.models.product_variant import ProductVariant

pytestmark = pytest.mark.integration

G_NS = "http://base.google.com/ns/1.0"
GN = f"{{{G_NS}}}"


def _make_variant(db_session, product, **overrides) -> ProductVariant:
    defaults = {
        "product_id": product.id,
        "title": "Red / M",
        "option_signature": f"sig-{product.id}-{overrides.get('sku', 'x')}",
        "stock": 5,
        "is_active": True,
    }
    defaults.update(overrides)
    variant = ProductVariant(**defaults)
    db_session.add(variant)
    db_session.commit()
    db_session.refresh(variant)
    return variant


def _items(xml_text: str) -> list[ET.Element]:
    root = ET.fromstring(xml_text)
    return root.findall("./channel/item")


def _g(item: ET.Element, name: str) -> str | None:
    el = item.find(f"{GN}{name}")
    return el.text if el is not None else None


def _link(item: ET.Element) -> str:
    """The item's plain ``<link>`` text (asserted present by these tests)."""
    el = item.find("link")
    assert el is not None and el.text is not None
    return el.text


# --- basics ----------------------------------------------------------------


async def test_feed_is_public_and_valid_xml(client, product_factory):
    product_factory(slug="feed-a", price_usd=19.99)

    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    # Parses without raising → well-formed XML.
    root = ET.fromstring(resp.text)
    assert root.tag == "rss"
    assert root.get("version") == "2.0"


async def test_feed_declares_g_namespace(client, product_factory):
    product_factory(slug="feed-ns", price_usd=19.99)

    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")

    # The g: namespace must be present (declared on root or used on items).
    assert 'xmlns:g="http://base.google.com/ns/1.0"' in resp.text
    items = _items(resp.text)
    assert len(items) == 1
    assert _g(items[0], "id") is not None


# --- selection: active + priced --------------------------------------------


async def test_only_active_products_emitted(client, product_factory):
    product_factory(slug="feed-active", price_usd=10.0)
    product_factory(slug="feed-inactive", price_usd=10.0, is_active=False)

    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")

    links = [_link(item) for item in _items(resp.text)]
    assert any("feed-active" in url for url in links)
    assert not any("feed-inactive" in url for url in links)


async def test_currency_filtering_excludes_unpriced(client, product_factory):
    # Priced in PLN/EUR only — must NOT appear in a USD feed.
    product_factory(slug="feed-no-usd", price_usd=None, price_pln=80.0, price_eur=18.0)
    product_factory(slug="feed-has-usd", price_usd=25.0, price_pln=None, price_eur=None)

    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")

    links = [_link(item) for item in _items(resp.text)]
    assert any("feed-has-usd" in url for url in links)
    assert not any("feed-no-usd" in url for url in links)


async def test_price_format_and_currency(client, product_factory):
    product_factory(slug="feed-price", price_pln=199.0)

    resp = await client.get("/api/v1/feeds/google/PL.xml?lang=pl&currency=PLN")

    item = _items(resp.text)[0]
    assert _g(item, "price") == "199.00 PLN"


# --- id scheme -------------------------------------------------------------


async def test_id_scheme_uses_sku_when_set(client, product_factory):
    product_factory(slug="feed-sku", sku="MY-SKU-1", price_usd=10.0)

    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")

    item = _items(resp.text)[0]
    assert _g(item, "id") == "MY-SKU-1"


async def test_id_scheme_falls_back_to_prod_id(client, product_factory):
    product = product_factory(slug="feed-nosku", sku=None, price_usd=10.0)

    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")

    item = _items(resp.text)[0]
    assert _g(item, "id") == f"prod_{product.id}"


# --- availability ----------------------------------------------------------


async def test_availability_in_stock(client, product_factory):
    product_factory(slug="feed-instock", stock=5, price_usd=10.0)
    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")
    assert _g(_items(resp.text)[0], "availability") == "in_stock"


async def test_availability_backorder(client, product_factory):
    product_factory(slug="feed-bo", stock=0, allow_backorder=True, price_usd=10.0)
    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")
    assert _g(_items(resp.text)[0], "availability") == "backorder"


async def test_availability_out_of_stock(client, product_factory):
    product_factory(slug="feed-oos", stock=0, allow_backorder=False, price_usd=10.0)
    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")
    assert _g(_items(resp.text)[0], "availability") == "out_of_stock"


# --- identifier_exists -----------------------------------------------------


async def test_identifier_exists_no_when_no_ids(client, product_factory):
    product_factory(slug="feed-noid", sku="S1", brand=None, gtin=None, mpn=None, price_usd=10.0)
    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")
    item = _items(resp.text)[0]
    assert _g(item, "identifier_exists") == "no"


async def test_identifier_exists_omitted_when_brand_set(client, product_factory):
    product_factory(slug="feed-brand", brand="Acme", gtin=None, mpn=None, price_usd=10.0)
    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")
    item = _items(resp.text)[0]
    assert _g(item, "identifier_exists") is None
    assert _g(item, "brand") == "Acme"


# --- PL language fallback --------------------------------------------------


async def test_pl_language_uses_pl_fields(client, product_factory):
    product_factory(
        slug="feed-pl",
        name="English Name",
        name_pl="Polska Nazwa",
        price_pln=50.0,
    )

    resp = await client.get("/api/v1/feeds/google/PL.xml?lang=pl&currency=PLN")

    item = _items(resp.text)[0]
    assert _g(item, "title") == "Polska Nazwa"
    # Link is PL-prefixed and carries the feed currency.
    assert "/pl/products/feed-pl" in _link(item)
    assert "currency=PLN" in _link(item)


async def test_pl_language_falls_back_to_en_when_pl_null(client, product_factory):
    product_factory(slug="feed-plfb", name="Only English", name_pl=None, price_pln=50.0)

    resp = await client.get("/api/v1/feeds/google/PL.xml?lang=pl&currency=PLN")

    assert _g(_items(resp.text)[0], "title") == "Only English"


async def test_en_link_is_unprefixed(client, product_factory):
    product_factory(slug="feed-en-link", price_usd=10.0)
    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")
    link = _link(_items(resp.text)[0])
    assert "/products/feed-en-link" in link
    assert "/en/products/" not in link
    assert "currency=USD" in link


# --- images ----------------------------------------------------------------


async def test_images_primary_and_additional(client, product_factory, db_session):
    product = product_factory(slug="feed-img", price_usd=10.0)
    db_session.add_all(
        [
            ProductImage(
                product_id=product.id,
                url="https://cdn.example.com/primary.webp",
                sort_order=0,
                is_primary=True,
                width=800,
                height=600,
                bytes=1000,
                mime="image/webp",
            ),
            ProductImage(
                product_id=product.id,
                url="https://cdn.example.com/extra.webp",
                sort_order=1,
                is_primary=False,
                width=800,
                height=600,
                bytes=1000,
                mime="image/webp",
            ),
        ]
    )
    db_session.commit()

    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")
    item = _items(resp.text)[0]
    assert _g(item, "image_link") == "https://cdn.example.com/primary.webp"
    extras = [el.text for el in item.findall(f"{GN}additional_image_link")]
    assert extras == ["https://cdn.example.com/extra.webp"]


# --- variants --------------------------------------------------------------


async def test_variant_product_emits_one_item_per_variant_with_group_id(
    client, product_factory, db_session
):
    product = product_factory(slug="feed-var", sku="GRP-1", price_usd=20.0)
    _make_variant(db_session, product, title="Red", sku="RED-1", stock=3)
    _make_variant(db_session, product, title="Blue", sku="BLUE-1", stock=0)

    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")

    items = _items(resp.text)
    # Two variant items, no separate variantless item.
    assert len(items) == 2
    ids = {_g(i, "id") for i in items}
    assert ids == {"RED-1", "BLUE-1"}
    # Both share the product's feed id as item_group_id.
    group_ids = {_g(i, "item_group_id") for i in items}
    assert group_ids == {"GRP-1"}
    # Variant availability follows variant stock.
    by_id = {_g(i, "id"): i for i in items}
    assert _g(by_id["RED-1"], "availability") == "in_stock"
    assert _g(by_id["BLUE-1"], "availability") == "out_of_stock"
    # Variant title appended to the product name.
    assert _g(by_id["RED-1"], "title") == f"{product.name} - Red"


async def test_variant_inherits_product_price(client, product_factory, db_session):
    product = product_factory(slug="feed-var-price", sku="VP-1", price_usd=20.0)
    # NULL variant price → inherits product price.
    _make_variant(db_session, product, sku="VP-A", price_usd=None)

    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")

    item = _items(resp.text)[0]
    assert _g(item, "price") == "20.00 USD"


async def test_variant_falls_back_to_prod_id_when_no_sku(client, product_factory, db_session):
    product = product_factory(slug="feed-var-noid", sku=None, price_usd=20.0)
    variant = _make_variant(db_session, product, sku=None)

    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")

    item = _items(resp.text)[0]
    assert _g(item, "id") == f"prod_{product.id}_v{variant.id}"
    assert _g(item, "item_group_id") == f"prod_{product.id}"


# --- google_product_category inheritance -----------------------------------


async def test_google_product_category_inherits_from_category(
    client, product_factory, category_factory
):
    cat = category_factory(slug="feed-cat", google_product_category="166")
    product_factory(slug="feed-gpc", category=cat, price_usd=10.0)

    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")

    assert _g(_items(resp.text)[0], "google_product_category") == "166"


async def test_product_google_product_category_override(client, product_factory, category_factory):
    cat = category_factory(slug="feed-cat2", google_product_category="166")
    product_factory(
        slug="feed-gpc2",
        category=cat,
        google_product_category="500",
        price_usd=10.0,
    )

    resp = await client.get("/api/v1/feeds/google/US.xml?lang=en&currency=USD")

    assert _g(_items(resp.text)[0], "google_product_category") == "500"
