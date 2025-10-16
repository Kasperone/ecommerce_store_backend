from typing import Optional
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate
from app.core.security import get_password_hash, verify_password


class CRUDUser(CRUDBase[User, UserCreate, UserUpdate]):
    """CRUD operations for User model"""
    
    def get_by_email(self, db: Session, *, email: str) -> Optional[User]:
        """
        Get user by email
        
        Args:
            db: Database session
            email: User email
        
        Returns:
            User object or None
        """
        return db.query(User).filter(User.email == email).first()
    
    def create(self, db: Session, *, obj_in: UserCreate) -> User:
        """
        Create new user with hashed password
        
        Args:
            db: Database session
            obj_in: User creation schema
        
        Returns:
            Created user object
        """
        db_obj = User(
            email=obj_in.email,
            hashed_password=get_password_hash(obj_in.password),
            first_name=obj_in.first_name,
            last_name=obj_in.last_name,
            phone=obj_in.phone,
        )
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj
    
    def authenticate(
        self, db: Session, *, email: str, password: str
    ) -> Optional[User]:
        """
        Authenticate user with email and password
        
        Args:
            db: Database session
            email: User email
            password: Plain text password
        
        Returns:
            User object if authenticated, None otherwise
        """
        user = self.get_by_email(db, email=email)
        if not user:
            return None
        if not verify_password(password, user.hashed_password):
            return None
        return user
    
    def is_active(self, user: User) -> bool:
        """Check if user is active"""
        return user.is_active
    
    def is_admin(self, user: User) -> bool:
        """Check if user is admin"""
        from app.models.user import UserRole
        return user.role == UserRole.ADMIN


user = CRUDUser(User)
