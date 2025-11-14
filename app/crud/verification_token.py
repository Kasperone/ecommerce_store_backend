from typing import Optional
from datetime import datetime
from sqlalchemy.orm import Session

from app.crud.base import CRUDBase
from app.models.verification_token import VerificationToken


class CRUDVerificationToken(CRUDBase[VerificationToken, None, None]):
    """CRUD operations for VerificationToken model"""
    
    def create_for_user(
        self, 
        db: Session, 
        *, 
        user_id: int, 
        hours: int = 24
    ) -> VerificationToken:
        """
        Create a new verification token for a user
        
        Args:
            db: Database session
            user_id: User ID
            hours: Token validity in hours (default 24)
        
        Returns:
            Created VerificationToken object
        """
        token_str = VerificationToken.generate_token()
        expires_at = VerificationToken.get_expiration_time(hours=hours)
        
        db_obj = VerificationToken(
            token=token_str,
            user_id=user_id,
            expires_at=expires_at
        )
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj
    
    def get_by_token(
        self, 
        db: Session, 
        *, 
        token: str
    ) -> Optional[VerificationToken]:
        """
        Get verification token by token string
        
        Args:
            db: Database session
            token: Token string
        
        Returns:
            VerificationToken object or None
        """
        return db.query(VerificationToken).filter(
            VerificationToken.token == token
        ).first()
    
    def get_by_user_id(
        self, 
        db: Session, 
        *, 
        user_id: int
    ) -> Optional[VerificationToken]:
        """
        Get the most recent verification token for a user
        
        Args:
            db: Database session
            user_id: User ID
        
        Returns:
            Most recent VerificationToken object or None
        """
        return db.query(VerificationToken).filter(
            VerificationToken.user_id == user_id
        ).order_by(VerificationToken.created_at.desc()).first()
    
    def delete_by_token(
        self, 
        db: Session, 
        *, 
        token: str
    ) -> bool:
        """
        Delete verification token by token string
        
        Args:
            db: Database session
            token: Token string
        
        Returns:
            True if deleted, False if not found
        """
        db_obj = self.get_by_token(db, token=token)
        if db_obj:
            db.delete(db_obj)
            db.commit()
            return True
        return False
    
    def delete_by_user_id(
        self, 
        db: Session, 
        *, 
        user_id: int
    ) -> int:
        """
        Delete all verification tokens for a user
        
        Args:
            db: Database session
            user_id: User ID
        
        Returns:
            Number of tokens deleted
        """
        count = db.query(VerificationToken).filter(
            VerificationToken.user_id == user_id
        ).delete()
        db.commit()
        return count
    
    def cleanup_expired(self, db: Session) -> int:
        """
        Delete all expired verification tokens
        
        Args:
            db: Database session
        
        Returns:
            Number of tokens deleted
        """
        count = db.query(VerificationToken).filter(
            VerificationToken.expires_at < datetime.utcnow()
        ).delete()
        db.commit()
        return count
    
    def is_valid(
        self, 
        db: Session, 
        *, 
        token: str
    ) -> tuple[bool, Optional[str]]:
        """
        Check if token is valid (exists and not expired)
        
        Args:
            db: Database session
            token: Token string
        
        Returns:
            Tuple of (is_valid, error_message)
        """
        db_obj = self.get_by_token(db, token=token)
        
        if not db_obj:
            return False, "Invalid verification token"
        
        if db_obj.is_expired:
            return False, "Verification token has expired"
        
        return True, None


verification_token = CRUDVerificationToken(VerificationToken)
