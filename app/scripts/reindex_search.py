"""Rebuild the Meilisearch indexes from Postgres.

    python -m app.scripts.reindex_search

Configures both indexes (settings + primary key) and rebuilds them from the
current DB state. This is the initial-seed step after
``docker compose up -d meilisearch`` and the drift-recovery safety net for any
write that happened while Meilisearch was down. Only ``is_active`` rows are
indexed, so drafts/inactive content never leaks into search.

Unlike the per-write sync helpers (which are best-effort and swallow errors),
this is an explicit operator action: it exits non-zero on a config/engine error
so problems surface loudly.
"""

import logging
import sys

from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.core.search import (
    CATEGORIES_INDEX,
    PRODUCTS_INDEX,
    build_client,
    meili_configured,
)
from app.models.category import Category
from app.models.product import Product
from app.services.search_index import (
    build_category_document,
    build_product_document,
    configure_indexes,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("reindex_search")


def reindex() -> None:
    if not meili_configured():
        logger.error(
            "Meilisearch is not configured — set MEILISEARCH_URL and "
            "MEILISEARCH_MASTER_KEY in .env. Aborting."
        )
        sys.exit(1)

    configure_indexes()
    client = build_client()
    db = SessionLocal()
    try:
        # --- products ---
        products = db.execute(select(Product).where(Product.is_active.is_(True))).scalars().all()
        product_docs = [build_product_document(p) for p in products]
        product_index = client.index(PRODUCTS_INDEX)
        product_index.delete_all_documents()
        if product_docs:
            product_index.add_documents(product_docs, primary_key="id")

        # --- categories (with active-product counts in one grouped query) ---
        count_rows = db.execute(
            select(Product.category_id, func.count())
            .where(Product.is_active.is_(True))
            .group_by(Product.category_id)
        ).all()
        counts: dict[int, int] = {
            category_id: total for category_id, total in count_rows if category_id is not None
        }
        categories = (
            db.execute(select(Category).where(Category.is_active.is_(True))).scalars().all()
        )
        category_docs = [
            build_category_document(c, product_count=counts.get(c.id, 0)) for c in categories
        ]
        category_index = client.index(CATEGORIES_INDEX)
        category_index.delete_all_documents()
        if category_docs:
            category_index.add_documents(category_docs, primary_key="id")

        logger.info(
            "Reindex queued: %d products, %d categories",
            len(product_docs),
            len(category_docs),
        )
    finally:
        db.close()


if __name__ == "__main__":
    reindex()
