"""Best-effort Meilisearch index sync.

Every ``sync_*`` / ``delete_*`` function here is **best-effort**:

* it returns immediately (no-op) when Meilisearch is unconfigured — this is what
  keeps the test/CI/dev suites green without a running engine, and lets the GHCR
  ``:main`` image boot; and
* it swallows + logs any engine error, so a down/slow Meilisearch never fails or
  slows the originating request. ``app.scripts.reindex_search`` is the safety net
  that rebuilds the index for any write that happened while the engine was down.

Only ``is_active`` products/categories are ever indexed — drafts and inactive
rows are deleted from the index, so nothing sensitive lives there even though it
is backend-private.

``configure_indexes()`` is the one function that is *not* best-effort: it is an
explicit operator action (run from the reindex script) and raises so config
problems surface loudly.
"""

import logging
from datetime import UTC

from meilisearch.errors import MeilisearchApiError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.money import as_float
from app.core.search import (
    CATEGORIES_INDEX,
    PRODUCTS_INDEX,
    build_client,
    meili_configured,
)
from app.crud import category as crud_category
from app.crud import product as crud_product
from app.models.category import Category
from app.models.product import Product

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Index settings — searchable/filterable/sortable/ranking/synonyms/locales.
# ---------------------------------------------------------------------------

# Searchable order is the relevance weighting: earlier attributes rank higher.
PRODUCT_INDEX_SETTINGS: dict = {
    "searchableAttributes": [
        "name",
        "name_pl",
        "sku",
        "category_name",
        "category_name_pl",
        "description",
        "description_pl",
        # Flattened option values (EN+PL, e.g. "Red", "XL", "Czerwony") so a
        # query like "red shirt" matches variant products.
        "option_values",
        # Merchandising tag names so a free-text query can match a tag label.
        "tags",
    ],
    "filterableAttributes": [
        "category_id",
        "in_stock",
        "price_usd",
        "price_pln",
        "price_eur",
        "is_featured",
        # For a future facet sidebar over option values (out of scope in v1 —
        # /api/v1/search's build_filters is untouched).
        "option_values",
        # Merchandising tag names — drives the /api/v1/search tag filter + facet.
        "tags",
        # Slugs of the manual collections this product belongs to — drives the
        # /api/v1/search collection filter + facet.
        "collection_slugs",
    ],
    "sortableAttributes": ["price_usd", "price_pln", "price_eur", "created_at", "name"],
    # Meili defaults + a featured boost appended (applies within the "sort" tie).
    "rankingRules": [
        "words",
        "typo",
        "proximity",
        "attribute",
        "sort",
        "exactness",
        "is_featured:desc",
    ],
    "typoTolerance": {
        "enabled": True,
        "minWordSizeForTypos": {"oneTypo": 5, "twoTypos": 9},
    },
    # Per-attribute tokenization: Polish stop-words/segmentation on the *_pl
    # fields, English on the rest. ISO-639-3 locale codes (Meili v1.10+).
    "localizedAttributes": [
        {
            "locales": ["pol"],
            "attributePatterns": ["name_pl", "description_pl", "category_name_pl"],
        },
        {
            "locales": ["eng"],
            "attributePatterns": ["name", "description", "category_name", "sku"],
        },
    ],
    # Small seed set; admin-tunable later from real query logs.
    "synonyms": {
        "phone": ["mobile", "smartphone"],
        "mobile": ["phone", "smartphone"],
        "smartphone": ["phone", "mobile"],
        "laptop": ["notebook"],
        "notebook": ["laptop"],
        "headphones": ["earphones", "słuchawki"],
        "earphones": ["headphones"],
        "słuchawki": ["headphones", "earphones"],
    },
}

CATEGORY_INDEX_SETTINGS: dict = {
    "searchableAttributes": ["name", "name_pl", "parent_name"],
    "localizedAttributes": [
        {"locales": ["pol"], "attributePatterns": ["name_pl"]},
        {"locales": ["eng"], "attributePatterns": ["name", "parent_name"]},
    ],
}


# ---------------------------------------------------------------------------
# Document builders — pure functions over a loaded ORM object (unit-tested).
# ---------------------------------------------------------------------------


def _primary_image_url(product: Product) -> str | None:
    """URL of the primary image, else the first by sort_order, else None."""
    images = product.images  # relationship is ordered by sort_order
    if not images:
        return None
    for image in images:
        if image.is_primary:
            return image.url
    return images[0].url


def _epoch(value) -> int | None:
    """Naive-UTC datetime → epoch seconds (Meili sorts numerically)."""
    if value is None:
        return None
    return int(value.replace(tzinfo=UTC).timestamp())


def _min_variant_price(product: Product, attr: str) -> float | None:
    """Min effective price across active variants (inherit-aware), else the
    product column.

    For a variant product the indexed price becomes the "from" price — keeps
    the existing filter/sort semantics meaningful. A NULL variant price
    inherits the product's; all-NULL candidates collapse to None (not for
    sale in that currency).
    """
    # Money columns are Decimal; the search document must be JSON-serializable
    # (Meilisearch), so emit float.
    active = [variant for variant in product.variants if variant.is_active]
    if not active:
        return as_float(getattr(product, attr))
    candidates = [
        price
        for price in (
            getattr(variant, attr) if getattr(variant, attr) is not None else getattr(product, attr)
            for variant in active
        )
        if price is not None
    ]
    return as_float(min(candidates)) if candidates else None


def _option_values(product: Product) -> list[str]:
    """Flattened option values (EN + PL, option order then value order)."""
    values: list[str] = []
    for option in product.options:
        for value in option.values:
            values.append(value.value)
            if value.value_pl:
                values.append(value.value_pl)
    return values


def _tag_names(product: Product) -> list[str]:
    """Member tag names (single-locale merchandising labels)."""
    return [tag.name for tag in product.tags]


def _collection_slugs(product: Product) -> list[str]:
    """Slugs of the manual collections this product belongs to."""
    return [collection.slug for collection in product.collections]


def build_product_document(product: Product) -> dict:
    """Flatten a product (+ its category + primary image + variants) into one
    Meili doc."""
    category = product.category
    return {
        "id": product.id,
        "name": product.name,
        "name_pl": product.name_pl,
        "description": product.description,
        "description_pl": product.description_pl,
        "slug": product.slug,
        "sku": product.sku,
        "price_usd": _min_variant_price(product, "price_usd"),
        "price_pln": _min_variant_price(product, "price_pln"),
        "price_eur": _min_variant_price(product, "price_eur"),
        "category_id": product.category_id,
        "category_name": category.name if category else None,
        "category_name_pl": category.name_pl if category else None,
        "image_url": _primary_image_url(product),
        # product.stock is already the sum across variants for variant products.
        "in_stock": product.stock > 0,
        "is_featured": product.is_featured,
        "stock": product.stock,
        "has_variants": product.has_variants,
        "option_values": _option_values(product),
        "tags": _tag_names(product),
        "collection_slugs": _collection_slugs(product),
        "created_at": _epoch(product.created_at),
    }


def build_category_document(category: Category, *, product_count: int) -> dict:
    """Flatten a category (+ its parent name + product count) into one Meili doc."""
    parent = category.parent
    return {
        "id": category.id,
        "name": category.name,
        "name_pl": category.name_pl,
        "slug": category.slug,
        "slug_pl": category.slug_pl,
        "parent_name": parent.name if parent else None,
        "product_count": product_count,
    }


def _count_active_products_in_category(db: Session, category_id: int) -> int:
    return db.execute(
        select(func.count())
        .select_from(Product)
        .where(Product.category_id == category_id, Product.is_active.is_(True))
    ).scalar_one()


# ---------------------------------------------------------------------------
# Index configuration (explicit operator action — raises on error).
# ---------------------------------------------------------------------------


def configure_indexes() -> None:
    """Create both indexes (if absent) and apply their settings.

    Caller must ensure ``meili_configured()``. Raises on any engine error so
    misconfiguration surfaces loudly when run from the reindex script.
    """
    client = build_client()
    for name, index_settings in (
        (PRODUCTS_INDEX, PRODUCT_INDEX_SETTINGS),
        (CATEGORIES_INDEX, CATEGORY_INDEX_SETTINGS),
    ):
        try:
            client.create_index(name, {"primaryKey": "id"})
        except MeilisearchApiError as exc:
            # Re-running configure on an existing index is expected and fine.
            if exc.code != "index_already_exists":
                raise
        client.index(name).update_settings(index_settings)


# ---------------------------------------------------------------------------
# Best-effort sync operations (no-op when unconfigured; swallow + log on error).
# ---------------------------------------------------------------------------


def sync_product(db: Session, product_id: int) -> None:
    """Upsert an active product into the index, or delete it if gone/inactive."""
    if not meili_configured():
        return
    try:
        product = crud_product.get(db, id=product_id)
        index = build_client().index(PRODUCTS_INDEX)
        if product is None or not product.is_active:
            index.delete_document(str(product_id))
        else:
            index.add_documents([build_product_document(product)], primary_key="id")
    except Exception:
        logger.warning("Meili sync_product(%s) failed", product_id, exc_info=True)


def delete_product(product_id: int) -> None:
    """Remove a product from the index (used on the DELETE route)."""
    if not meili_configured():
        return
    try:
        build_client().index(PRODUCTS_INDEX).delete_document(str(product_id))
    except Exception:
        logger.warning("Meili delete_product(%s) failed", product_id, exc_info=True)


def sync_products_bulk(db: Session, product_ids: list[int]) -> None:
    """Re-sync many products in one call (upsert active, delete gone/inactive)."""
    if not meili_configured() or not product_ids:
        return
    try:
        loaded = {pid: crud_product.get(db, id=pid) for pid in dict.fromkeys(product_ids)}
        docs = [build_product_document(p) for p in loaded.values() if p is not None and p.is_active]
        stale = [str(pid) for pid, p in loaded.items() if p is None or not p.is_active]
        index = build_client().index(PRODUCTS_INDEX)
        if docs:
            index.add_documents(docs, primary_key="id")
        if stale:
            index.delete_documents(stale)
    except Exception:
        logger.warning("Meili sync_products_bulk(%s) failed", product_ids, exc_info=True)


def sync_products_for_category(db: Session, category_id: int) -> None:
    """Re-sync every active product in a category — used when the category's
    denormalized name (``category_name``) changes."""
    if not meili_configured():
        return
    try:
        ids = (
            db.execute(
                select(Product.id).where(
                    Product.category_id == category_id, Product.is_active.is_(True)
                )
            )
            .scalars()
            .all()
        )
        sync_products_bulk(db, list(ids))
    except Exception:
        logger.warning("Meili sync_products_for_category(%s) failed", category_id, exc_info=True)


def sync_products_for_collection(db: Session, collection_id: int) -> None:
    """Re-sync every active member product of a collection — used when the
    collection is renamed (its ``collection_slugs`` on member docs change)."""
    if not meili_configured():
        return
    try:
        from app.models.collection import collection_products

        ids = (
            db.execute(
                select(Product.id)
                .join(collection_products, collection_products.c.product_id == Product.id)
                .where(
                    collection_products.c.collection_id == collection_id,
                    Product.is_active.is_(True),
                )
            )
            .scalars()
            .all()
        )
        sync_products_bulk(db, list(ids))
    except Exception:
        logger.warning(
            "Meili sync_products_for_collection(%s) failed", collection_id, exc_info=True
        )


def sync_category(db: Session, category_id: int) -> None:
    """Upsert an active category into the index, or delete it if gone/inactive."""
    if not meili_configured():
        return
    try:
        category = crud_category.get(db, id=category_id)
        index = build_client().index(CATEGORIES_INDEX)
        if category is None or not category.is_active:
            index.delete_document(str(category_id))
        else:
            count = _count_active_products_in_category(db, category_id)
            index.add_documents(
                [build_category_document(category, product_count=count)], primary_key="id"
            )
    except Exception:
        logger.warning("Meili sync_category(%s) failed", category_id, exc_info=True)


def delete_category(category_id: int) -> None:
    """Remove a category from the index (used on the DELETE route)."""
    if not meili_configured():
        return
    try:
        build_client().index(CATEGORIES_INDEX).delete_document(str(category_id))
    except Exception:
        logger.warning("Meili delete_category(%s) failed", category_id, exc_info=True)


def sync_products_in_background(product_ids: list[int]) -> None:
    """Background-task entry point for the order hot path.

    Opens its own session (the request's session is closed by the time a
    FastAPI ``BackgroundTask`` runs) so checkout latency is never coupled to
    Meilisearch.
    """
    if not meili_configured() or not product_ids:
        return
    db = SessionLocal()
    try:
        sync_products_bulk(db, product_ids)
    finally:
        db.close()
