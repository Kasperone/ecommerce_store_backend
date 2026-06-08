"""Google Shopping product feed builder (Pillar E of the Google-Ads plan).

Emits a Google Shopping **RSS 2.0** document with the ``g:`` namespace
(``http://base.google.com/ns/1.0``) that Merchant Center fetches on a schedule.
One feed is built per (country, language, currency) tuple — Google requires the
feed currency to match the target country, and our per-currency price columns
make the feed price exactly equal the PDP/checkout price (no runtime FX).

Identifier scheme (locked — must match GA4 ``item_id`` and the Ads ``id``):
``product.sku`` if set, else ``prod_{product.id}``. For variant products one
``<item>`` is emitted per variant sharing ``g:item_group_id`` = the product id,
each variant's own ``id`` = ``variant.sku`` if set else ``{product_id}_v{vid}``.

The XML is built with :mod:`xml.etree.ElementTree` so every text node is escaped
by the serializer — nothing is hand-concatenated.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from app.core.config import settings
from app.models.product import Product
from app.models.product_variant import ProductVariant

# The Google Shopping RSS namespace. RSS items put product attributes under the
# ``g:`` prefix; the channel itself stays plain RSS 2.0.
G_NS = "http://base.google.com/ns/1.0"

# Currency code -> the product/variant price-column attribute name.
_PRICE_ATTR = {
    "USD": "price_usd",
    "PLN": "price_pln",
    "EUR": "price_eur",
}

# Google title hard limit.
_TITLE_MAX = 150


def product_link(slug: str, lang: str, currency: str) -> str:
    """Absolute storefront URL for a product's PDP, in the feed's locale.

    The EN locale is UNPREFIXED (matches the storefront i18n:
    ``/products/<slug>`` for en, ``/pl/products/<slug>`` for pl). A
    ``?currency=`` query is appended so the landing page renders the same
    currency the feed advertises (the storefront honours a currency query).
    """
    base = settings.FRONTEND_URL.rstrip("/")
    prefix = "" if lang == "en" else f"/{lang}"
    return f"{base}{prefix}/products/{slug}?currency={currency.upper()}"


def _localized(value_en: str | None, value_pl: str | None, lang: str) -> str | None:
    """Pick the PL field when ``lang == 'pl'`` (EN fallback when PL is null)."""
    if lang == "pl" and value_pl:
        return value_pl
    return value_en


def _truncate_title(title: str) -> str:
    """Clamp a title to Google's 150-char limit on a whitespace boundary."""
    if len(title) <= _TITLE_MAX:
        return title
    clipped = title[:_TITLE_MAX].rsplit(" ", 1)[0]
    # A single >150-char word leaves ``clipped`` empty — fall back to a hard cut.
    return clipped or title[:_TITLE_MAX]


def _price_string(amount: float, currency: str) -> str:
    """Google's ``<amount> <ISO currency>`` price format, e.g. ``199.00 PLN``."""
    return f"{amount:.2f} {currency.upper()}"


def _product_price(product: Product, currency: str) -> float | None:
    """The product's price in ``currency`` (None when unset for that column)."""
    return getattr(product, _PRICE_ATTR[currency.upper()])


def _variant_price(variant: ProductVariant, currency: str) -> float | None:
    """Variant effective price in ``currency`` (NULL inherits the product's)."""
    attr = {
        "USD": "effective_price_usd",
        "PLN": "effective_price_pln",
        "EUR": "effective_price_eur",
    }[currency.upper()]
    return getattr(variant, attr)


def _availability(stock: int, allow_backorder: bool) -> str:
    """Google availability value from stock + backorder policy."""
    if stock > 0:
        return "in_stock"
    if allow_backorder:
        return "backorder"
    return "out_of_stock"


def _product_type(product: Product, lang: str) -> str | None:
    """Derive ``g:product_type`` from the product's category name."""
    category = product.category
    if category is None:
        return None
    return _localized(category.name, category.name_pl, lang)


def _sub(parent: ET.Element, tag: str, text: str | None) -> None:
    """Append ``<tag>text</tag>`` to ``parent`` only when text is non-empty."""
    if text is None or text == "":
        return
    el = ET.SubElement(parent, tag)
    el.text = text


def _g(parent: ET.Element, name: str, text: str | None) -> None:
    """Append a ``g:``-namespaced child only when text is non-empty."""
    if text is None or text == "":
        return
    el = ET.SubElement(parent, f"{{{G_NS}}}{name}")
    el.text = text


def _images(product: Product) -> tuple[str | None, list[str]]:
    """(primary image url, additional image urls) — primary first, by sort.

    ProductImage rows are already ordered by ``sort_order`` (relationship
    order_by); the ``is_primary`` row, when present, leads.
    """
    urls = [img.url for img in product.images]
    if not urls:
        return None, []
    primary = next((img.url for img in product.images if img.is_primary), urls[0])
    additional = [u for u in urls if u != primary]
    return primary, additional


def _identifier_exists(brand: str | None, gtin: str | None, mpn: str | None) -> str | None:
    """``no`` when none of gtin/mpn/brand is set; omit the attribute otherwise.

    Google defaults ``identifier_exists`` to ``yes``; we only emit the explicit
    ``no`` so an item without any unique identifier isn't disapproved.
    """
    if not brand and not gtin and not mpn:
        return "no"
    return None


def _emit_common(
    item: ET.Element,
    *,
    product: Product,
    lang: str,
    description: str | None,
    link: str,
) -> None:
    """Attributes shared by variant and variantless items."""
    _sub(item, "link", link)
    _g(item, "description", description)
    _g(item, "condition", product.condition)
    _g(item, "product_type", _product_type(product, lang))
    gpc = product.google_product_category or (
        product.category.google_product_category if product.category else None
    )
    _g(item, "google_product_category", gpc)


def _emit_identity(item: ET.Element, product: Product) -> None:
    """Emit brand/gtin/mpn + the derived ``identifier_exists`` for a product."""
    _g(item, "brand", product.brand)
    _g(item, "gtin", product.gtin)
    _g(item, "mpn", product.mpn)
    _g(item, "identifier_exists", _identifier_exists(product.brand, product.gtin, product.mpn))


def build_item(
    channel: ET.Element,
    product: Product,
    *,
    lang: str,
    currency: str,
) -> int:
    """Append one or more ``<item>`` elements for ``product`` to ``channel``.

    Returns the number of items emitted (0 when the product has no price in
    ``currency`` — for a variant product, 0 when no active variant is priced).
    """
    name = _localized(product.name, product.name_pl, lang) or product.name
    description = _localized(product.description, product.description_pl, lang)
    primary_image, extra_images = _images(product)
    link = product_link(product.slug, lang, currency)
    product_feed_id = product.sku or f"prod_{product.id}"

    active_variants = product.active_variants
    if active_variants:
        emitted = 0
        for variant in active_variants:
            price = _variant_price(variant, currency)
            if price is None:
                continue
            item = ET.SubElement(channel, "item")
            variant_id = variant.sku or f"prod_{product.id}_v{variant.id}"
            _g(item, "id", variant_id)
            _g(item, "item_group_id", product_feed_id)

            variant_title = _localized(variant.title, variant.title_pl, lang)
            title = f"{name} - {variant_title}" if variant_title else name
            _g(item, "title", _truncate_title(title))

            _emit_common(item, product=product, lang=lang, description=description, link=link)

            # Variant-specific image, falling back to the product's primary.
            image = variant.image_url or primary_image
            _g(item, "image_link", image)
            for extra in extra_images:
                _g(item, "additional_image_link", extra)

            _g(item, "availability", _availability(variant.stock, product.allow_backorder))
            _g(item, "price", _price_string(price, currency))
            _emit_identity(item, product)
            emitted += 1
        return emitted

    # Variantless product — a single item, no item_group_id.
    price = _product_price(product, currency)
    if price is None:
        return 0
    item = ET.SubElement(channel, "item")
    _g(item, "id", product_feed_id)
    _g(item, "title", _truncate_title(name))
    _emit_common(item, product=product, lang=lang, description=description, link=link)
    _g(item, "image_link", primary_image)
    for extra in extra_images:
        _g(item, "additional_image_link", extra)
    _g(item, "availability", _availability(product.stock, product.allow_backorder))
    _g(item, "price", _price_string(price, currency))
    _emit_identity(item, product)
    return 1


def build_feed(
    products: list[Product],
    *,
    country: str,
    lang: str,
    currency: str,
) -> bytes:
    """Build the full RSS 2.0 + ``g:`` feed for ``products`` as UTF-8 bytes.

    Only items with a price in ``currency`` are included (handled per product
    in :func:`build_item`). ``country`` is used only in the channel title.
    """
    ET.register_namespace("g", G_NS)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    store_name = getattr(settings, "APP_NAME", "Store")
    _sub(channel, "title", f"{store_name} — {country.upper()} ({currency.upper()})")
    _sub(channel, "link", settings.FRONTEND_URL.rstrip("/"))
    _sub(
        channel,
        "description",
        f"Google Shopping product feed ({lang}/{currency.upper()}/{country.upper()})",
    )

    for product in products:
        build_item(channel, product, lang=lang, currency=currency)

    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)
