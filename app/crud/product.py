from sqlalchemy import nulls_last, or_
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.product import Product
from app.schemas.product import ProductCreate, ProductUpdate

# Whitelisted sort columns for the public product list. Keys are the API-facing
# ``sort_by`` values; resolving through this map (never interpolating the column
# name) keeps the query param from reaching arbitrary SQL.
SORTABLE_COLUMNS = {
    "created_at": Product.created_at,
    "price_usd": Product.price_usd,
    "name": Product.name,
}


class CRUDProduct(CRUDBase[Product, ProductCreate, ProductUpdate]):
    """CRUD operations for Product model"""

    def get_by_slug(self, db: Session, *, slug: str) -> Product | None:
        """
        Get product by slug

        Args:
            db: Database session
            slug: Product slug

        Returns:
            Product object or None
        """
        return db.query(Product).filter(Product.slug == slug).first()

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

        # Ordering — resolve through the whitelist so the param can't reach
        # arbitrary SQL. nulls_last keeps price-less products off the top of a
        # ``price_usd asc`` sort; the id tiebreak makes paging deterministic.
        column = SORTABLE_COLUMNS.get(sort_by, Product.created_at)
        ordering = column.asc() if order == "asc" else column.desc()
        query = query.order_by(nulls_last(ordering), Product.id.asc())

        # Pagination
        return query.offset(skip).limit(limit).all()

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

        return query.count()

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
            .filter(Product.is_featured.is_(True))
            .filter(Product.is_active.is_(True))
            .limit(limit)
            .all()
        )


product = CRUDProduct(Product)
