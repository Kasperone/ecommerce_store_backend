from datetime import timezone
from typing import Generator

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.security import decode_token
from app.crud import user as crud_user
from app.models.user import User, UserRole

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_current_user(
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
) -> User:
    """Resolve the bearer access token to a user.

    Rejects tokens whose ``iat`` predates ``user.password_changed_at`` —
    that's how /logout-all and password rotations invalidate access tokens
    without a denylist.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        decoded = decode_token(token, expected_type="access")
    except JWTError:
        raise credentials_exception

    try:
        user_id = int(decoded.subject)
    except ValueError:
        raise credentials_exception

    user = crud_user.get(db, id=user_id)
    if user is None:
        raise credentials_exception

    # password_changed_at is stored naive UTC; align the comparison.
    pwd_changed = user.password_changed_at
    if pwd_changed is not None:
        iat = decoded.issued_at
        if iat.tzinfo is not None:
            iat = iat.astimezone(timezone.utc).replace(tzinfo=None)
        # JWT iat is int seconds; floor pwd_changed (microseconds) to match.
        if iat < pwd_changed.replace(microsecond=0):
            raise credentials_exception

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user",
        )
    return current_user


async def get_current_admin(
    current_user: User = Depends(get_current_active_user),
) -> User:
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions",
        )
    return current_user
