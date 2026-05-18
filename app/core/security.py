"""Password hashing and JWT helpers.

Tokens carry a ``type`` claim (``"access"`` or ``"refresh"``) so the same
signing key cannot be used to swap one shape for the other. ``decode_token``
verifies signature, expiry, issuer, audience, and the expected type in a
single call. Each token also carries a unique ``jti`` (UUID4) which is used
by the refresh flow to look up the persisted DB row and detect reuse.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

TokenType = Literal["access", "refresh"]


# Password hashing context - Argon2id configuration
# Based on OWASP recommendations for Argon2id:
# - memory_cost: 19456 KiB (19 MiB)
# - time_cost: 2 iterations
# - parallelism: 1 thread
pwd_context = CryptContext(
    schemes=["argon2"],
    deprecated="auto",
    argon2__memory_cost=19456,
    argon2__time_cost=2,
    argon2__parallelism=1,
    argon2__type="ID",
)


@dataclass(frozen=True)
class DecodedToken:
    """Validated JWT payload, narrowed to the fields callers actually need."""

    subject: str
    jti: str
    type: TokenType
    issued_at: datetime
    expires_at: datetime


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _build_token(
    *,
    subject: str | Any,
    token_type: TokenType,
    expires_delta: timedelta,
    extra: dict | None = None,
) -> tuple[str, str, datetime]:
    """Build a signed JWT and return ``(token, jti, expires_at)``."""
    now = _utcnow()
    expires_at = now + expires_delta
    jti = uuid.uuid4().hex
    payload = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "jti": jti,
        "type": token_type,
    }
    if extra:
        payload.update(extra)
    encoded = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return encoded, jti, expires_at


def create_access_token(
    subject: str | Any, expires_delta: timedelta | None = None
) -> tuple[str, str, datetime]:
    """Issue a short-lived access token. Returns ``(token, jti, expires_at)``."""
    delta = expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return _build_token(subject=subject, token_type="access", expires_delta=delta)  # noqa: S106 -- token type label, not a credential


def create_refresh_token(
    subject: str | Any, expires_delta: timedelta | None = None
) -> tuple[str, str, datetime]:
    """Issue a refresh token. Returns ``(token, jti, expires_at)``.

    The caller is expected to persist the corresponding row in ``refresh_tokens``
    (storing the SHA-256 of ``token``) before returning anything to the client.
    """
    delta = expires_delta or timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    return _build_token(subject=subject, token_type="refresh", expires_delta=delta)  # noqa: S106 -- token type label, not a credential


def decode_token(token: str, *, expected_type: TokenType) -> DecodedToken:
    """Validate a JWT and require it to be of ``expected_type``.

    Raises :class:`jose.JWTError` on any failure (bad signature, wrong
    issuer/audience, expiry, missing claims, type mismatch). Callers in the
    auth layer translate this into a 401.
    """
    payload = jwt.decode(
        token,
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
        issuer=settings.JWT_ISSUER,
        audience=settings.JWT_AUDIENCE,
    )
    token_type = payload.get("type")
    if token_type != expected_type:
        raise JWTError(f"expected token type {expected_type!r}, got {token_type!r}")
    sub = payload.get("sub")
    jti = payload.get("jti")
    iat = payload.get("iat")
    exp = payload.get("exp")
    if not sub or not jti or iat is None or exp is None:
        raise JWTError("token is missing required claims")
    return DecodedToken(
        subject=str(sub),
        jti=str(jti),
        type=token_type,
        issued_at=datetime.fromtimestamp(int(iat), tz=UTC),
        expires_at=datetime.fromtimestamp(int(exp), tz=UTC),
    )


def hash_refresh_token(token: str) -> str:
    """SHA-256 hex digest used to store refresh tokens at rest. Lookup is by
    ``jti`` (cheap, indexed); the hash is then compared in constant time so a
    DB read alone cannot mint a valid token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)
