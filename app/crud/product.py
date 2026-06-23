from typing import Any

from sqlalchemy import func, nulls_last, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.crud.base import CRUDBase
from app.models.collection import collection_products
from app.models.order import Order, OrderItem, OrderStatus
from app.models.product import Product
from app.models.product_tag import ProductTag, product_tag_links
from app.models.product_variant import ProductOption, ProductVariant
from app.schemas.product import ProductCreate, ProductUpdate

# Whitelisted sort columns for the public product list. Keys are the API-facing
# ``sort_by`` values; resolving through this map (never interpolating the column
# name) keeps the query param from reaching arbitrary SQL.
SORTABLE_COLUMNS = {
    "created_at": Product.created_at,
    "price_usd": Product.price_usd,
    "name": Product.name,
}


# Detail-path eager loads: the PDP serializes options (with values) + active
# variants (with their option-value links), so the detail CRUD reads pull them
# in two selectin passes instead of lazy N+1s at serialization time.
_DETAIL_OPTIONS = (
    selectinload(Product.options).selectinload(ProductOption.values),
    selectinload(Product.variants).selectinload(ProductVariant.option_values),
)


class CRUDProduct(CRUDBase[Product, ProductCreate, ProductUpdate]):
    """CRUD operations for Product model"""

    def create(self, db: Session, *, obj_in: ProductCreate) -> Product:
        """Create a product, ignoring the write-only ``tags`` field.

        ``tags`` is a list of names, not a column — the route reconciles the
        ``product_tag_links`` join separately (after the audit record, to keep
        the column diff clean). Excluding it here keeps ``Product(**data)`` from
        trying to assign strings to the relationship.
        """
        data = obj_in.model_dump(exclude={"tags"})
        db_obj = Product(**data)
        db.add(db_obj)
        db.flush()
        db.refresh(db_obj)
        return db_obj

    def update(
        self, db: Session, *, db_obj: Product, obj_in: ProductUpdate | dict[str, Any]
    ) -> Product:
        """Update a product, ignoring the write-only ``tags`` field.

        Mirrors ``create``: ``tags`` is a list of names reconciled by the route
        (after the audit diff), not a column. Excluding it keeps
        ``CRUDBase.update`` from assigning tag-name strings onto the ``tags``
        relationship — which raises ``'str' has no attribute '_sa_instance_state'``
        once the relationship is loaded on the instance.
        """
        if isinstance(obj_in, dict):
            data = {k: v for k, v in obj_in.items() if k != "tags"}
        else:
            data = obj_in.model_dump(exclude_unset=True, exclude={"tags"})
        return super().update(db, db_obj=db_obj, obj_in=data)

    def get(self, db: Session, id: Any) -> Product | None:
        """Get a single product by ID with the variant matrix eager-loaded."""
        return db.query(Product).options(*_DETAIL_OPTIONS).filter(Product.id == id).first()

    def get_by_slug(self, db: Session, *, slug: str) -> Product | None:
        """
        Get product by slug (variant matrix eager-loaded for the PDP)

        Args:
            db: Database session
            slug: Product slug

        Returns:
            Product object or None
        """
        return db.query(Product).options(*_DETAIL_OPTIONS).filter(Product.slug == slug).first()

    def get_by_sku(self, db: Session, *, sku: str) -> Product | None:
        """
        Get product by SKU

        Args:
            db: Database session
            sku: Product SKU

        Returns:
            Product object or None
        """
        return db.query(Product).filter(Product.sku == sku).first()

    def get_multi_with_filters(
        self,
        db: Session,
        *,
        skip: int = 0,
        limit: int = 100,
        category_id: int | None = None,
        is_active: bool | None = True,
        is_featured: bool | None = None,
        min_price_usd: float | None = None,
        max_price_usd: float | None = None,
        in_stock: bool | None = None,
        search: str | None = None,
        tag: str | None = None,
        collection_id: int | None = None,
        sort_by: str = "created_at",
        order: str = "desc",
    ) -> list[Product]:
        """
        Get products with filters and pagination

        Args:
            db: Database session
            skip: Offset for pagination
            limit: Limit for pagination
            category_id: Filter by category ID
            is_active: Filter by active status
            is_featured: Filter by featured status
            min_price_usd: Minimum price in USD
            max_price_usd: Maximum price in USD
            in_stock: Filter by stock availability
            search: Search in name and description
            sort_by: Column to sort by (created_at, price_usd, name)
            order: Sort direction (asc or desc)

        Returns:
            List of products
        """
        query = db.query(Product)

        # Filter by active status
        if is_active is not None:
            query = query.filter(Product.is_active == is_active)

        # Filter by category
        if category_id:
            query = query.filter(Product.category_id == category_id)

        # Filter by featured
        if is_featured is not None:
            query = query.filter(Product.is_featured == is_featured)

        # Filter by price range
        if min_price_usd is not None:
            query = query.filter(Product.price_usd >= min_price_usd)
        if max_price_usd is not None:
            query = query.filter(Product.price_usd <= max_price_usd)

        # Filter by stock
        if in_stock is not None:
            if in_stock:
                query = query.filter(Product.stock > 0)
            else:
                query = query.filter(Product.stock == 0)

        # Search in name and description
        if search:
            search_filter = or_(
                Product.name.ilike(f"%{search}%"),
                Product.description.ilike(f"%{search}%"),
            )
            query = query.filter(search_filter)

        # Filter by tag name (case-insensitive) via the product_tag_links join.
        if tag:
            query = self._apply_tag_filter(query, tag)

        # Filter by collection membership via the collection_products join.
        if collection_id is not None:
            query = query.join(
                collection_products, collection_products.c.product_id == Product.id
            ).filter(collection_products.c.collection_id == collection_id)

        # Ordering. A collection query is ordered by the curated ``position`` (the
        # admin's display order); otherwise resolve the requested sort through the
        # whitelist so the param can't reach arbitrary SQL. nulls_last keeps
        # price-less products off the top of a ``price_usd asc`` sort; the id
        # tiebreak makes paging deterministic.
        if collection_id is not None:
            query = query.order_by(collection_products.c.position.asc(), Product.id.asc())
        else:
            column = SORTABLE_COLUMNS.get(sort_by, Product.created_at)
            ordering = column.asc() if order == "asc" else column.desc()
            query = query.order_by(nulls_last(ordering), Product.id.asc())

        # The list response carries ``has_variants`` (a property over
        # Product.variants) + the ``tags`` chips — selectin passes instead of
        # per-row lazy loads.
        query = query.options(selectinload(Product.variants), selectinload(Product.tags))

        # Pagination
        return query.offset(skip).limit(limit).all()

    def get_for_export(
        self,
        db: Session,
        *,
        category_id: int | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> list[Product]:
        """Fetch products (unpaginated) for the admin CSV export.

        Mirrors the admin product-list filters (category / active / search) so the
        export matches the on-screen filtered view. Eager-loads tags, collections,
        the category, and variants so the per-row CSV assembly (``has_variants`` +
        packed ``tags`` / ``collection_slugs`` + ``category_slug``) never N+1s.
        ``is_active`` defaults to ``None`` here (export all by default — the route
        owns the "active only" default if any), unlike the public list.
        """
        query = db.query(Product)

        if is_active is not None:
            query = query.filter(Product.is_active == is_active)
        if category_id:
            query = query.filter(Product.category_id == category_id)
        if search:
            query = query.filter(
                or_(
                    Product.name.ilike(f"%{search}%"),
                    Product.description.ilike(f"%{search}%"),
                )
            )

        query = query.options(
            selectinload(Product.variants),
            selectinload(Product.tags),
            selectinload(Product.collections),
            joinedload(Product.category),
        ).order_by(Product.id.asc())

        return query.all()

    def count_with_filters(
        self,
        db: Session,
        *,
        category_id: int | None = None,
        is_active: bool | None = True,
        is_featured: bool | None = None,
        min_price_usd: float | None = None,
        max_price_usd: float | None = None,
        in_stock: bool | None = None,
        search: str | None = None,
        tag: str | None = None,
        collection_id: int | None = None,
    ) -> int:
        """
        Count products with filters

        Args:
            Same as get_multi_with_filters

        Returns:
            Total count of filtered products
        """
        query = db.query(Product)

        if is_active is not None:
            query = query.filter(Product.is_active == is_active)
        if category_id:
            query = query.filter(Product.category_id == category_id)
        if is_featured is not None:
            query = query.filter(Product.is_featured == is_featured)
        if min_price_usd is not None:
            query = query.filter(Product.price_usd >= min_price_usd)
        if max_price_usd is not None:
            query = query.filter(Product.price_usd <= max_price_usd)
        if in_stock is not None:
            if in_stock:
                query = query.filter(Product.stock > 0)
            else:
                query = query.filter(Product.stock == 0)
        if search:
            search_filter = or_(
                Product.name.ilike(f"%{search}%"),
                Product.description.ilike(f"%{search}%"),
            )
            query = query.filter(search_filter)
        if tag:
            query = self._apply_tag_filter(query, tag)
        if collection_id is not None:
            query = query.join(
                collection_products, collection_products.c.product_id == Product.id
            ).filter(collection_products.c.collection_id == collection_id)

        return query.count()

    @staticmethod
    def _apply_tag_filter(query: Any, tag: str) -> Any:
        """Constrain a Product query to rows carrying ``tag`` (case-insensitive).

        Joins through ``product_tag_links`` → ``product_tags``; the name is
        whitespace-collapsed + lowercased and compared to ``lower(name)`` (the
        functional-unique index column), so the match is exact and CI.
        """
        normalized = " ".join(tag.split()).lower()
        return (
            query.join(product_tag_links, product_tag_links.c.product_id == Product.id)
            .join(ProductTag, ProductTag.id == product_tag_links.c.tag_id)
            .filter(func.lower(ProductTag.name) == normalized)
        )

    def get_featured(self, db: Session, *, limit: int = 10) -> list[Product]:
        """
        Get featured products

        Args:
            db: Database session
            limit: Maximum number of products

        Returns:
            List of featured products
        """
        return (
            db.query(Product)
            .options(selectinload(Product.variants))
            .filter(Product.is_featured.is_(True))
            .filter(Product.is_active.is_(True))
            .limit(limit)
            .all()
        )

    def get_related(self, db: Session, *, product: Product, limit: int = 8) -> list[Product]:
        """Ranked related products for a PDP ("customers also bought").

        Primary signal — **co-occurrence**: other products that appear in the
        same orders as ``product``, ranked by the number of distinct orders they
        share with it (descending), tie-broken by newest product id first. Only
        non-cancelled orders count (CANCELLED orders are excluded — they never
        represent a real "customers also bought" signal).

        Fallback fill — when co-occurrence yields fewer than ``limit`` products,
        the remainder is filled with other **active, in-stock** products from the
        **same category** (newest first), excluding the subject and anything
        already chosen.

        The subject product is always excluded, inactive products are never
        returned, results are de-duplicated and capped at ``limit``. Ranking is
        at PRODUCT grain — ``variant_id`` is ignored (co-occurrence is on
        ``product_id``). The card relationships the storefront needs
        (``images`` for the primary image, ``variants`` for ``has_variants``)
        are eager-loaded so serialisation never lazy-loads per row.
        """
        # 1. Orders (non-cancelled) that contain the subject product.
        subject_orders = (
            select(OrderItem.order_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(OrderItem.product_id == product.id)
            .where(Order.status != OrderStatus.CANCELLED)
        )

        # 2. Other active products in those orders, ranked by distinct-order
        #    co-occurrence count (desc), then newest product id (desc).
        cooccurrence = (
            db.query(OrderItem.product_id)
            .join(Product, Product.id == OrderItem.product_id)
            .filter(OrderItem.order_id.in_(subject_orders))
            .filter(OrderItem.product_id != product.id)
            .filter(Product.is_active.is_(True))
            .group_by(OrderItem.product_id)
            .order_by(
                func.count(func.distinct(OrderItem.order_id)).desc(),
                OrderItem.product_id.desc(),
            )
            .limit(limit)
            .all()
        )
        ranked_ids: list[int] = [row[0] for row in cooccurrence]

        # 3. Fallback: fill the remainder from the same category if short.
        if len(ranked_ids) < limit and product.category_id is not None:
            exclude = {*ranked_ids, product.id}
            fallback = (
                db.query(Product.id)
                .filter(Product.category_id == product.category_id)
                .filter(Product.is_active.is_(True))
                .filter(Product.stock > 0)
                .filter(Product.id.not_in(exclude))
                .order_by(Product.created_at.desc(), Product.id.desc())
                .limit(limit - len(ranked_ids))
                .all()
            )
            ranked_ids.extend(row[0] for row in fallback)

        if not ranked_ids:
            return []

        # 4. Load the chosen rows in one pass (with card relationships eager) and
        #    restore the ranked order — an ``IN`` query won't preserve it.
        products = (
            db.query(Product)
            .options(selectinload(Product.images), selectinload(Product.variants))
            .filter(Product.id.in_(ranked_ids))
            .all()
        )
        by_id = {p.id: p for p in products}
        return [by_id[pid] for pid in ranked_ids if pid in by_id]

    def get_feed_entries(self, db: Session) -> list[Product]:
        """All active products for the Google Shopping feed (Pillar E).

        Eager-loads everything the feed serializer touches per product —
        images, category, options (with values), and variants (with their
        option-value links) — so the feed builds in a fixed number of queries
        regardless of catalogue size. Currency-presence filtering happens in
        the feed builder (a product is emitted only when it has a price in the
        requested currency), not here. ``id`` order keeps output stable.
        """
        return (
            db.query(Product)
            .options(
                selectinload(Product.images),
                joinedload(Product.category),
                selectinload(Product.options).selectinload(ProductOption.values),
                selectinload(Product.variants).selectinload(ProductVariant.option_values),
            )
            .filter(Product.is_active.is_(True))
            .order_by(Product.id)
            .all()
        )

    def get_sitemap_entries(self, db: Session) -> list[Product]:
        """
        All active products with their images eagerly loaded, for the public
        sitemap-source endpoint (SEO blueprint D7).

        Unpaginated by design: the consumer is the frontend's sitemap
        generator, which needs the complete catalog in one pass. ``id`` order
        keeps output stable between calls.
        """
        return (
            db.query(Product)
            .options(selectinload(Product.images))
            .filter(Product.is_active.is_(True))
            .order_by(Product.id)
            .all()
        )


product = CRUDProduct(Product)
