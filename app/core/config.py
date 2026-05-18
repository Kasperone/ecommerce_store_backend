from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Both `SECRET_KEY` and `JWT_SECRET_KEY` are HMAC keys; OWASP guidance is
# >= 256 bits (32 bytes) of entropy. Enforced at startup so a misconfigured
# env never silently weakens token signing.
_MIN_SECRET_BYTES = 32


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    # Application
    APP_NAME: str = "E-commerce API"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = True
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    SECRET_KEY: str

    # Database
    DATABASE_URL: str

    # JWT
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14
    JWT_ISSUER: str = "ecommerce-store"
    JWT_AUDIENCE: str = "ecommerce-store-clients"

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # Rate limiting (slowapi). Use ``memory://`` for single-instance dev,
    # ``redis://host:port/db`` for multi-worker / production deployments.
    RATE_LIMIT_STORAGE_URL: str = "memory://"

    # Cloudflare R2
    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET_NAME: str = ""
    R2_PUBLIC_URL: str = ""

    # Email / Resend
    RESEND_API_KEY: str = ""
    EMAIL_FROM: str = "noreply@ecommerce.com"
    EMAIL_FROM_NAME: str = "E-commerce Store"
    FRONTEND_URL: str = "http://localhost:3000"

    # PayU
    PAYU_POS_ID: str = ""
    PAYU_CLIENT_ID: str = ""
    PAYU_CLIENT_SECRET: str = ""
    PAYU_SANDBOX: bool = True

    # Stripe
    STRIPE_SECRET_KEY: str = ""
    STRIPE_PUBLISHABLE_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="allow")

    @field_validator("SECRET_KEY", "JWT_SECRET_KEY")
    @classmethod
    def _enforce_secret_length(cls, value: str, info) -> str:
        if len(value.encode("utf-8")) < _MIN_SECRET_BYTES:
            raise ValueError(
                f"{info.field_name} must be at least {_MIN_SECRET_BYTES} bytes "
                f"(got {len(value.encode('utf-8'))}). Generate with "
                f"`python -c 'import secrets; print(secrets.token_urlsafe(48))'`."
            )
        return value


settings = Settings()  # pyright: ignore[reportCallIssue] -- pydantic-settings loads required fields from env
