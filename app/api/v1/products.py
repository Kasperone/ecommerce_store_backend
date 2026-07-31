from math import ceil
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.capabilities import CATALOG_WRITE
from app.core.dependencies import (
    get_current_admin,
    get_db,
    get_optional_user,
    require_capability,
)
from app.core.money import as_float
from app.crud import price_history as crud_price_history
from app.crud import product as crud_product
from app.crud import product_tag as crud_product_tag
from app.crud.product_tag import normalize_name
from app.models.order import OrderItem
from app.models.price_history import SUPPORTED_CURRENCIES
from app.models.product import Product
from app.models.quote import QuoteItem
from app.models.user import User, UserRole
from app.schemas.price_history import LowestPriceResponse
from app.schemas.product import (
    ProductCreate,
    ProductListResponse,
    ProductResponse,
    ProductUpdate,
    ProductWithCategory,
)
from app.services import search_index
from app.services.audit import AuditWriter, get_audit_writer
from app.services.b2b_pricing import annotate_products_for_user
from app.services.translation_admin import purge_translation_meta

router = APIRouter()

# The catalog write/DELETE routes below (create/update/delete) live in this
# public-facing router alongside the storefront read routes. They must carry the
# same capability fence as the admin catalog tools in ``admin_products.py`` — a
# scoped staffer without ``catalog:write`` must not be able to mutate products.
_catalog_write = Depends(require_capability(CATALOG_WRITE))


def _reconcile_product_tags(db: Session, product: Product, names: list[str]) -> None:
    """Replace ``product.tags`` with the get-or-create vocabulary rows for ``names``.

    Names are trimmed / whitespace-collapsed and de-duplicated case-insensitively;
    blanks are dropped. Each surviving name is resolved through
    ``crud_product_tag.get_or_create`` (case-insensitive), so existing tags are
    reused and new ones created on the fly. The join is fully replaced — passing
    ``[]`` clears every tag.

    Must run AFTER ``audit.record_update`` on the product: ``get_or_create`` may
    flush (creating a new tag row), which would clear the ``get_history`` the
    audit writer reads for the product column diff.
    """
    resolved: list = []
    seen: set[str] = set()
    for raw in names:
        norm = normalize_name(raw)
        if not norm:
            continue
        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        resolved.append(crud_product_tag.get_or_create(db, name=norm))
    product.tags = resolved


@router.get("/", response_model=ProductListResponse)
async def list_products(
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    category_id: int | None = Query(None, description="Filter by category ID"),
    is_active: bool | None = Query(
        None,
        description=(
            "Filter by active status. Admin-only — non-admin callers always "
            "receive active products regardless of this value (omit = all, for admins)."
        ),
    ),
    is_featured: bool | None = Query(None, description="Filter by featured status"),
    min_price: float | None = Query(None, ge=0, description="Minimum price (USD)"),
    max_price: float | None = Query(None, ge=0, description="Maximum price (USD)"),
    in_stock: bool | None = Query(None, description="Filter by stock availability"),
    search: str | None = Query(None, max_length=100, description="Search in name and description"),
    tag: str | None = Query(
        None, max_length=50, description="Filter by tag name (case-insensitive)"
    ),
    collection_id: int | None = Query(
        None, description="Filter by collection membership (ordered by curation position)"
    ),
    sort_by: Literal["created_at", "price_usd", "name"] = Query(
        "created_at", description="Field to sort by"
    ),
    order: Literal["asc", "desc"] = Query("desc", description="Sort direction"),
):
    """
    Get paginated list of products with filters

    - **page**: Page number (starts from 1)
    - **page_size**: Number of items per page (1-100)
    - **category_id**: Filter by category
    - **is_active**: Admin-only filter. Non-admins always get active products;
      an admin may pass ``false`` (inactive only) or omit it for all
    - **is_featured**: Filter featured products
    - **min_price/max_price**: Price range filter (USD)
    - **in_stock**: Filter by stock availability
    - **search**: Search query for name and description
    - **sort_by**: Field to sort by (created_at, price_usd, name)
    - **order**: Sort direction (asc or desc)
    """
    # This list endpoint is public. Only admins may see inactive products (or the
    # unfiltered "all" view via is_active=None); for every other caller we force
    # the active-only filter regardless of what was requested, so a guest can
    # never enumerate inactive / unpublished products through this route.
    if current_user is None or current_user.role != UserRole.ADMIN:
        is_active = True

    skip = (page - 1) * page_size

    # Get products with filters
    products = crud_product.get_multi_with_filters(
        db,
        skip=skip,
        limit=page_size,
        category_id=category_id,
        is_active=is_active,
        is_featured=is_featured,
        min_price_usd=min_price,
        max_price_usd=max_price,
        in_stock=in_stock,
        search=search,
        tag=tag,
        collection_id=collection_id,
        sort_by=sort_by,
        order=order,
    )

    # Get total count
    total = crud_product.count_with_filters(
        db,
        category_id=category_id,
        is_active=is_active,
        is_featured=is_featured,
        min_price_usd=min_price,
        max_price_usd=max_price,
        in_stock=in_stock,
        search=search,
        tag=tag,
        collection_id=collection_id,
    )

    pages = ceil(total / page_size) if total > 0 else 1

    # B2B: stamp negotiated (price-list) prices onto each product for an active
    # company member — no-op for guests / non-B2B callers (fields stay None).
    annotate_products_for_user(
        db, products=products, user_id=current_user.id if current_user else None
    )

    return {
        "items": products,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.get("/featured", response_model=list[ProductResponse])
async def get_featured_products(
    db: Session = Depends(get_db),
    limit: int = Query(10, ge=1, le=50, description="Maximum number of products"),
):
    """
    Get featured products

    - **limit**: Maximum number of products to return (1-50)
    """
    products = crud_product.get_featured(db, limit=limit)
    return products


@router.get("/{product_id}", response_model=ProductWithCategory)
async def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
):
    """
    Get product by ID with category details
    """
    product = crud_product.get(db, id=product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    annotate_products_for_user(
        db, products=[product], user_id=current_user.id if current_user else None
    )
    return product


@router.get("/slug/{slug}", response_model=ProductWithCategory)
async def get_product_by_slug(
    slug: str,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
):
    """
    Get product by slug with category details.

    Never-existed slug -> 404; deactivated product -> 410 Gone. The distinct
    410 tells crawlers to drop the URL faster than a 404 (SEO blueprint D12)
    and stops delisted products from being publicly reachable by direct slug.
    Admin surfaces fetch by integer ID and are unaffected.
    """
    product = crud_product.get_by_slug(db, slug=slug)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    if not product.is_active:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Product no longer available")
    annotate_products_for_user(
        db, products=[product], user_id=current_user.id if current_user else None
    )
    return product


@router.get("/{product_id}/lowest-30d", response_model=LowestPriceResponse)
async def get_lowest_30d(
    product_id: int,
    currency: Literal["USD", "PLN", "EUR"] = Query(
        ..., description="Currency to scope the lookup to"
    ),
    variant_id: int | None = Query(
        None, description="Scope the lookup to one variant's own price trail"
    ),
    db: Session = Depends(get_db),
):
    """
    OMNIBUS (Directive 98/6 Art. 6a) — lowest price for ``product_id`` in
    ``currency`` over the last 30 days.

    Public + unauthenticated: the answer is what the storefront PDP needs to
    legally show next to a reduced price. ``variant_id`` scopes the lookup to
    that variant's own per-offer trail (Omnibus is per-offer); omitted, the
    product-grain trail answers — today's behavior. Returns
    ``lowest_price = null`` + ``since = null`` when no history exists for that
    (product[, variant], currency) — the storefront then renders no comparison
    line.
    """
    product = crud_product.get(db, id=product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    if variant_id is not None and not any(variant.id == variant_id for variant in product.variants):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found")

    result = crud_price_history.lowest_in_last_30_days(
        db, product_id=product.id, currency=currency, variant_id=variant_id
    )
    if result is None:
        return LowestPriceResponse(currency=currency, lowest_price=None, since=None)
    lowest, since = result
    return LowestPriceResponse(currency=currency, lowest_price=as_float(lowest), since=since)


@router.get("/{slug}/related", response_model=list[ProductResponse])
async def get_related_products(
    slug: str,
    db: Session = Depends(get_db),
    limit: int = Query(8, ge=1, le=24, description="Maximum number of related products"),
):
    """
    Related products for the PDP ("You may also like" / "Customers also bought").

    Public + unauthenticated. Resolves ``slug`` like the product-detail route
    (404 if the slug is unknown or the product is inactive). Returns a ranked
    list of ``ProductResponse`` cards — the same shape the storefront product
    listing renders — so the frontend reuses its existing product-card
    component.

    Ranking (computed at read time, no new table):

    - **Primary — "customers also bought":** other products that co-appear in
      the same non-cancelled orders, ranked by the number of distinct orders
      they share with this product (desc), newest product id as a tiebreak.
    - **Fallback fill:** if fewer than ``limit`` co-occurrence results, the rest
      is filled with other active, in-stock products from the same category
      (newest first), excluding the subject and any already chosen.

    The subject is always excluded; inactive products are never returned;
    results are de-duped and capped at ``limit``. Returns ``[]`` when a product
    has neither co-occurrence nor same-category peers.
    """
    product = crud_product.get_by_slug(db, slug=slug)
    if not product or not product.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return crud_product.get_related(db, product=product, limit=limit)


@router.post(
    "/",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_catalog_write],
)
async def create_product(
    product_in: ProductCreate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """
    Create new product (Admin only)

    Requires admin authentication
    """
    # Check if slug already exists
    existing = crud_product.get_by_slug(db, slug=product_in.slug)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Product with this slug already exists"
        )

    # Check if SKU already exists (if provided)
    if product_in.sku:
        existing_sku = crud_product.get_by_sku(db, sku=product_in.sku)
        if existing_sku:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Product with this SKU already exists",
            )

    product = crud_product.create(db, obj_in=product_in)
    audit.record_create(resource_type="product", obj=product)
    # OMNIBUS (Directive 98/6 Art. 6a): record the new product's prices so the
    # 30-day-low query has an honest ground state. Same transaction as the
    # product write — rollback un-does both.
    for currency in SUPPORTED_CURRENCIES:
        crud_price_history.record_if_changed(
            db,
            product=product,
            currency=currency,
            new_price=getattr(product, f"price_{currency.lower()}"),
        )
    # Reconcile the tag join (get-or-create the vocabulary, sync links). Done
    # after the audit record so the product's column diff stays clean; the link
    # change is captured separately below.
    if product_in.tags is not None:
        _reconcile_product_tags(db, product, product_in.tags)
        audit.record_event(
            action="update",
            resource_type="product_tag_link",
            resource_id=product.id,
            payload={"tags": [t.name for t in product.tags]},
        )
    db.commit()
    db.refresh(product)
    # Best-effort: keep the search index in step (no-op when Meili unconfigured).
    search_index.sync_product(db, product.id)
    return product


@router.put("/{product_id}", response_model=ProductResponse, dependencies=[_catalog_write])
async def update_product(
    product_id: int,
    product_in: ProductUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """
    Update product (Admin only)

    Requires admin authentication
    """
    product = crud_product.get(db, id=product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    # Check slug uniqueness if being updated
    if product_in.slug and product_in.slug != product.slug:
        existing = crud_product.get_by_slug(db, slug=product_in.slug)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Product with this slug already exists",
            )

    # Check SKU uniqueness if being updated
    if product_in.sku and product_in.sku != product.sku:
        existing_sku = crud_product.get_by_sku(db, sku=product_in.sku)
        if existing_sku:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Product with this SKU already exists",
            )

    product = crud_product.update(db, db_obj=product, obj_in=product_in)
    audit.record_update(resource_type="product", obj=product)
    # OMNIBUS: append a price-history row for any currency whose price changed.
    # ``record_if_changed`` no-ops when the new price equals the most recent
    # row, so calling it for all three currencies on every PUT is safe (and the
    # idiomatic way to keep the chokepoint as the only writer). Must run AFTER
    # ``audit.record_update`` — its flush would otherwise clear ``get_history``
    # that the audit writer reads (banked from the mark_paid → audit ordering
    # in admin orders PATCH).
    for currency in SUPPORTED_CURRENCIES:
        crud_price_history.record_if_changed(
            db,
            product=product,
            currency=currency,
            new_price=getattr(product, f"price_{currency.lower()}"),
        )
    # Reconcile the tag join when ``tags`` was provided. ``None`` (omitted) leaves
    # tags unchanged; ``[]`` clears them. Must run AFTER ``audit.record_update``
    # (its flush would otherwise clear the product's ``get_history`` diff).
    if product_in.tags is not None:
        _reconcile_product_tags(db, product, product_in.tags)
        audit.record_event(
            action="update",
            resource_type="product_tag_link",
            resource_id=product.id,
            payload={"tags": [t.name for t in product.tags]},
        )
    db.commit()
    db.refresh(product)
    # Best-effort re-sync (drops the doc if the update flipped is_active off).
    search_index.sync_product(db, product.id)
    return product


@router.delete(
    "/{product_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_catalog_write]
)
async def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
    audit: AuditWriter = Depends(get_audit_writer),
):
    """
    Delete product (Admin only)

    Requires admin authentication
    """
    product = crud_product.get(db, id=product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    # Pre-check the meaningful business case: a product referenced by order or
    # quote history cannot be hard-deleted (``order_items.product_id`` is
    # ON DELETE RESTRICT / NO ACTION; ``quote_items.product_id`` is SET NULL but
    # we still refuse so the history stays attributable). Admins deactivate
    # (``is_active=False``) such products instead. Other references
    # (images, reviews, inventory, variants, wishlist, collections, price
    # history) are ON DELETE CASCADE and clean up with the product.
    if (
        db.query(OrderItem.id).filter(OrderItem.product_id == product_id).first() is not None
        or db.query(QuoteItem.id).filter(QuoteItem.product_id == product_id).first() is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Product has order or quote history; deactivate it instead.",
        )

    # Snapshot before delete — the writer needs ``obj`` readable.
    audit.record_delete(resource_type="product", obj=product)
    crud_product.delete(db, id=product_id)
    # The translation sidecar is polymorphic, so it carries no FK and nothing
    # at the database level cleans up after it.
    purge_translation_meta(db, resource_type="product", resource_ids=[product_id])
    # Defensive backstop: any FK reference the pre-check missed surfaces as an
    # IntegrityError on commit rather than a 500 — roll back and return 409.
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Product is referenced by other records; deactivate it instead.",
        ) from exc
    # Best-effort: drop the product from the search index.
    search_index.delete_product(product_id)
    return None
