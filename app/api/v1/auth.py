from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core.dependencies import get_db, get_current_user, get_current_active_user
from app.core.security import create_access_token
from app.core.config import settings
from app.core.email import send_verification_email, send_welcome_email
from app.crud import user as crud_user
from app.crud.verification_token import verification_token
from app.schemas.auth import Token, EmailVerification, EmailResend
from app.schemas.user import UserCreate, UserResponse, UserUpdate
from app.models.user import User

router = APIRouter()


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    user_in: UserCreate,
    db: Session = Depends(get_db),
):
    """
    Register a new user
    
    - **email**: Valid email address (unique)
    - **password**: Strong password (min 8 chars, 1 digit, 1 uppercase)
    - **first_name**: Optional first name
    - **last_name**: Optional last name
    - **phone**: Optional phone number
    """
    # Check if user already exists
    existing_user = crud_user.get_by_email(db, email=user_in.email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User with this email already exists"
        )
    
    # Create new user
    user = crud_user.create(db, obj_in=user_in)
    
    # Generate verification token and send email (if RESEND_API_KEY is configured)
    if settings.RESEND_API_KEY and settings.RESEND_API_KEY != "re_your_api_key_here":
        try:
            token = verification_token.create_for_user(db, user_id=user.id)
            send_verification_email(
                email_to=user.email,
                username=user.first_name or user.email,
                token=token.token
            )
        except Exception as e:
            # Log error but don't fail registration
            print(f"Failed to send verification email: {str(e)}")
    else:
        # Auto-verify user if email is not configured (development only)
        print("⚠️  RESEND_API_KEY not configured - auto-verifying user for development")
        user.is_verified = True
        db.commit()
        db.refresh(user)
    
    return user


@router.post("/login", response_model=Token)
async def login(
    db: Session = Depends(get_db),
    form_data: OAuth2PasswordRequestForm = Depends(),
):
    """
    OAuth2 compatible token login
    
    - **username**: User email
    - **password**: User password
    
    Returns JWT access token
    """
    # Authenticate user
    user = crud_user.authenticate(
        db, 
        email=form_data.username,
        password=form_data.password
    )
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user"
        )
    
    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified. Please check your email for verification link."
        )
    
    # Create access token
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        subject=user.id,
        expires_delta=access_token_expires
    )
    
    return {
        "access_token": access_token,
        "token_type": "bearer"
    }


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    current_user: User = Depends(get_current_active_user),
):
    """
    Get current user information
    
    Requires authentication
    """
    return current_user


@router.put("/me", response_model=UserResponse)
async def update_current_user(
    user_in: UserUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Update current user profile
    
    Requires authentication
    """
    user = crud_user.update(db, db_obj=current_user, obj_in=user_in)
    return user


@router.post("/refresh", response_model=Token)
async def refresh_token(
    current_user: User = Depends(get_current_user),
):
    """
    Refresh access token
    
    Requires valid token
    """
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        subject=current_user.id,
        expires_delta=access_token_expires
    )
    
    return {
        "access_token": access_token,
        "token_type": "bearer"
    }


@router.post("/verify-email", status_code=status.HTTP_200_OK)
async def verify_email(
    verification: EmailVerification,
    db: Session = Depends(get_db),
):
    """
    Verify user email with token
    
    - **token**: Verification token from email
    
    Returns success message
    """
    # Validate token
    is_valid, error_msg = verification_token.is_valid(db, token=verification.token)
    
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg
        )
    
    # Get token and user
    token_obj = verification_token.get_by_token(db, token=verification.token)
    if not token_obj:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid verification token"
        )
    
    user = crud_user.get(db, id=token_obj.user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Check if already verified
    if user.is_verified:
        return {"message": "Email already verified"}
    
    # Mark user as verified
    user.is_verified = True
    db.commit()
    
    # Delete verification token
    verification_token.delete_by_token(db, token=verification.token)
    
    # Send welcome email
    try:
        send_welcome_email(
            email_to=user.email,
            username=user.first_name or user.email
        )
    except Exception as e:
        print(f"Failed to send welcome email: {str(e)}")
    
    return {"message": "Email verified successfully"}


@router.post("/resend-verification", status_code=status.HTTP_200_OK)
async def resend_verification_email(
    resend: EmailResend,
    db: Session = Depends(get_db),
):
    """
    Resend verification email
    
    - **email**: User email address
    
    Returns success message
    """
    # Get user by email
    user = crud_user.get_by_email(db, email=resend.email)
    
    if not user:
        # Don't reveal if user exists or not (security)
        return {"message": "If the email exists, a verification link has been sent"}
    
    # Check if already verified
    if user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already verified"
        )
    
    # Delete old tokens for this user
    verification_token.delete_by_user_id(db, user_id=user.id)
    
    # Create new verification token
    token = verification_token.create_for_user(db, user_id=user.id)
    
    # Send verification email
    try:
        send_verification_email(
            email_to=user.email,
            username=user.first_name or user.email,
            token=token.token
        )
    except Exception as e:
        print(f"Failed to send verification email: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send verification email"
        )
    
    return {"message": "Verification email sent"}
