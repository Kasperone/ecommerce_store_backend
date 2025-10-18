from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.dependencies import get_db, get_current_admin
from app.crud import category as crud_category
from app.models.product import Product
from app.schemas.category import (
    CategoryCreate,
    CategoryUpdate,
    CategoryResponse,
    CategoryWithProductCount,
)

router = APIRouter()


@router.get("/", response_model=List[CategoryWithProductCount])
async def list_categories(
    db: Session = Depends(get_db),
    skip: int = Query(0, ge=0, description="Offset for pagination"),
    limit: int = Query(100, ge=1, le=100, description="Limit for pagination"),
    is_active: bool = Query(True, description="Filter by active status"),
):
    """
    Get list of categories with product counts
    
    - **skip**: Offset for pagination
    - **limit**: Number of items (1-100)
    - **is_active**: Show only active categories (default: true)
    """
    if is_active:
        categories = crud_category.category.get_active(db, skip=skip, limit=limit)
    else:
        categories = crud_category.category.get_multi(db, skip=skip, limit=limit)
    
    # Add product counts
    result = []
    for cat in categories:
        product_count = db.query(func.count(Product.id))\
            .filter(Product.category_id == cat.id)\
            .filter(Product.is_active == True)\
            .scalar()
        
        cat_dict = CategoryResponse.model_validate(cat).model_dump()
        cat_dict["product_count"] = product_count or 0
        result.append(cat_dict)
    
    return result


@router.get("/{category_id}", response_model=CategoryWithProductCount)
async def get_category(
    category_id: int,
    db: Session = Depends(get_db),
):
    """
    Get category by ID with product count
    """
    category = crud_category.category.get(db, id=category_id)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )
    
    # Get product count
    product_count = db.query(func.count(Product.id))\
        .filter(Product.category_id == category.id)\
        .filter(Product.is_active == True)\
        .scalar()
    
    cat_dict = CategoryResponse.model_validate(category).model_dump()
    cat_dict["product_count"] = product_count or 0
    
    return cat_dict


@router.get("/slug/{slug}", response_model=CategoryWithProductCount)
async def get_category_by_slug(
    slug: str,
    db: Session = Depends(get_db),
):
    """
    Get category by slug with product count
    """
    category = crud_category.category.get_by_slug(db, slug=slug)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )
    
    # Get product count
    product_count = db.query(func.count(Product.id))\
        .filter(Product.category_id == category.id)\
        .filter(Product.is_active == True)\
        .scalar()
    
    cat_dict = CategoryResponse.model_validate(category).model_dump()
    cat_dict["product_count"] = product_count or 0
    
    return cat_dict


@router.post("/", response_model=CategoryResponse, status_code=status.HTTP_201_CREATED)
async def create_category(
    category_in: CategoryCreate,
    db: Session = Depends(get_db),
    current_admin: dict = Depends(get_current_admin),
):
    """
    Create new category (Admin only)
    
    Requires admin authentication
    """
    # Check if slug already exists
    existing = crud_category.category.get_by_slug(db, slug=category_in.slug)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category with this slug already exists"
        )
    
    category = crud_category.category.create(db, obj_in=category_in)
    return category


@router.put("/{category_id}", response_model=CategoryResponse)
async def update_category(
    category_id: int,
    category_in: CategoryUpdate,
    db: Session = Depends(get_db),
    current_admin: dict = Depends(get_current_admin),
):
    """
    Update category (Admin only)
    
    Requires admin authentication
    """
    category = crud_category.category.get(db, id=category_id)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )
    
    # Check slug uniqueness if being updated
    if category_in.slug and category_in.slug != category.slug:
        existing = crud_category.category.get_by_slug(db, slug=category_in.slug)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Category with this slug already exists"
            )
    
    category = crud_category.category.update(db, db_obj=category, obj_in=category_in)
    return category


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: int,
    db: Session = Depends(get_db),
    current_admin: dict = Depends(get_current_admin),
):
    """
    Delete category (Admin only)
    
    Requires admin authentication
    """
    category = crud_category.category.get(db, id=category_id)
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )
    
    # Check if category has products
    product_count = db.query(func.count(Product.id))\
        .filter(Product.category_id == category.id)\
        .scalar()
    
    if product_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete category with {product_count} products. Remove products first."
        )
    
    crud_category.category.remove(db, id=category_id)
    return None