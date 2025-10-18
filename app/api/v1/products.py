from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from math import ceil

from app.core.dependencies import get_db, get_current_admin
from app.crud import product as crud_product
from app.schemas.product import (
    ProductCreate,
    ProductUpdate,
    ProductResponse,
    ProductWithCategory,
    ProductListResponse,
)

router = APIRouter()


@router.get("/", response_model=ProductListResponse)
async def list_products(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    category_id: Optional[int] = Query(None, description="Filter by category ID"),
    is_active: Optional[bool] = Query(True, description="Filter by active status"),
    is_featured: Optional[bool] = Query(None, description="Filter by featured status"),
    min_price: Optional[float] = Query(None, ge=0, description="Minimum price (USD)"),
    max_price: Optional[float] = Query(None, ge=0, description="Maximum price (USD)"),
    in_stock: Optional[bool] = Query(None, description="Filter by stock availability"),
    search: Optional[str] = Query(None, max_length=100, description="Search in name and description"),
):
    """
    Get paginated list of products with filters
    
    - **page**: Page number (starts from 1)
    - **page_size**: Number of items per page (1-100)
    - **category_id**: Filter by category
    - **is_active**: Show only active products (default: true)
    - **is_featured**: Filter featured products
    - **min_price/max_price**: Price range filter (USD)
    - **in_stock**: Filter by stock availability
    - **search**: Search query for name and description
    """
    skip = (page - 1) * page_size
    
    # Get products with filters
    products = crud_product.product.get_multi_with_filters(
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
    )
    
    # Get total count
    total = crud_product.product.count_with_filters(
        db,
        category_id=category_id,
        is_active=is_active,
        is_featured=is_featured,
        min_price_usd=min_price,
        max_price_usd=max_price,
        in_stock=in_stock,
        search=search,
    )
    
    pages = ceil(total / page_size) if total > 0 else 1
    
    return {
        "items": products,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


@router.get("/featured", response_model=List[ProductResponse])
async def get_featured_products(
    db: Session = Depends(get_db),
    limit: int = Query(10, ge=1, le=50, description="Maximum number of products"),
):
    """
    Get featured products
    
    - **limit**: Maximum number of products to return (1-50)
    """
    products = crud_product.product.get_featured(db, limit=limit)
    return products


@router.get("/{product_id}", response_model=ProductWithCategory)
async def get_product(
    product_id: int,
    db: Session = Depends(get_db),
):
    """
    Get product by ID with category details
    """
    product = crud_product.product.get(db, id=product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )
    return product


@router.get("/slug/{slug}", response_model=ProductWithCategory)
async def get_product_by_slug(
    slug: str,
    db: Session = Depends(get_db),
):
    """
    Get product by slug with category details
    """
    product = crud_product.product.get_by_slug(db, slug=slug)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )
    return product


@router.post("/", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
async def create_product(
    product_in: ProductCreate,
    db: Session = Depends(get_db),
    current_admin: dict = Depends(get_current_admin),
):
    """
    Create new product (Admin only)
    
    Requires admin authentication
    """
    # Check if slug already exists
    existing = crud_product.product.get_by_slug(db, slug=product_in.slug)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Product with this slug already exists"
        )
    
    # Check if SKU already exists (if provided)
    if product_in.sku:
        existing_sku = crud_product.product.get_by_sku(db, sku=product_in.sku)
        if existing_sku:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Product with this SKU already exists"
            )
    
    product = crud_product.product.create(db, obj_in=product_in)
    return product


@router.put("/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: int,
    product_in: ProductUpdate,
    db: Session = Depends(get_db),
    current_admin: dict = Depends(get_current_admin),
):
    """
    Update product (Admin only)
    
    Requires admin authentication
    """
    product = crud_product.product.get(db, id=product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )
    
    # Check slug uniqueness if being updated
    if product_in.slug and product_in.slug != product.slug:
        existing = crud_product.product.get_by_slug(db, slug=product_in.slug)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Product with this slug already exists"
            )
    
    # Check SKU uniqueness if being updated
    if product_in.sku and product_in.sku != product.sku:
        existing_sku = crud_product.product.get_by_sku(db, sku=product_in.sku)
        if existing_sku:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Product with this SKU already exists"
            )
    
    product = crud_product.product.update(db, db_obj=product, obj_in=product_in)
    return product


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_admin: dict = Depends(get_current_admin),
):
    """
    Delete product (Admin only)
    
    Requires admin authentication
    """
    product = crud_product.product.get(db, id=product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )
    
    crud_product.product.remove(db, id=product_id)
    return None