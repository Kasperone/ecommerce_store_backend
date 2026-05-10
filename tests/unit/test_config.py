"""Unit tests for ``app.core.config.Settings``.

The module-level validator on ``SECRET_KEY`` and ``JWT_SECRET_KEY`` must
fail fast at construction time so a misconfigured environment never
silently weakens token signing.
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


# Both secrets must be >= 32 bytes; pad with a clearly non-secret tail so
# the assertions are easy to scan.
_OK_SECRET = "test-secret-padding-padding-padding"
_OK_JWT_SECRET = "test-jwt-secret-padding-padding-padding"


def _build(**overrides) -> Settings:
    """Construct a Settings without reading the on-disk .env."""
    base = dict(
        ENVIRONMENT="development",
        SECRET_KEY=_OK_SECRET,
        JWT_SECRET_KEY=_OK_JWT_SECRET,
        DATABASE_URL="postgresql://x/y",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_settings_accepts_secrets_at_least_32_bytes():
    settings = _build()
    assert settings.SECRET_KEY == _OK_SECRET
    assert settings.JWT_SECRET_KEY == _OK_JWT_SECRET


def test_settings_rejects_short_secret_key():
    with pytest.raises(ValidationError) as excinfo:
        _build(SECRET_KEY="short")
    msg = str(excinfo.value)
    assert "SECRET_KEY" in msg
    assert "32" in msg


def test_settings_rejects_short_jwt_secret_key():
    with pytest.raises(ValidationError) as excinfo:
        _build(JWT_SECRET_KEY="too-short-for-hmac")
    msg = str(excinfo.value)
    assert "JWT_SECRET_KEY" in msg
    assert "32" in msg


def test_settings_rejects_secret_at_31_bytes_boundary():
    """Boundary check: 31 bytes is one short of the threshold and must fail."""
    thirty_one = "x" * 31
    with pytest.raises(ValidationError):
        _build(SECRET_KEY=thirty_one)


def test_settings_accepts_secret_at_32_bytes_boundary():
    thirty_two = "x" * 32
    settings = _build(SECRET_KEY=thirty_two)
    assert settings.SECRET_KEY == thirty_two
