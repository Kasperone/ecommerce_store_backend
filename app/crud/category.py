from typing import List, Optional
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.category import Category
from app.schemas.category import CategoryCreate, CategoryUpdate


class CRUDCategory(CRUDBase[Category, CategoryCreate, CategoryUpdate]):
    """CRUD operations for Category model"""
    
    def get_by_slug(self, db: Session, *, slug: str) -> Optional[Category]:
        """
        Get category by slug
        
        Args:
            db: Database session
            slug: Category slug
        
        Returns:
            Category object or None
        """
        return db.query(Category).filter(Category.slug == slug).first()
    
    def get_active(self, db: Session, *, skip: int = 0, limit: int = 100) -> List[Category]:
        """
        Get active categories
        
        Args:
            db: Database session
            skip: Offset for pagination
            limit: Limit for pagination
        
        Returns:
            List of active categories
        """
        return db.query(Category)\
            .filter(Category.is_active == True)\
            .offset(skip)\
            .limit(limit)\
            .all()


category = CRUDCategory(Category)
