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
    return Settings(_env_file=None, **base)  # pyright: ignore[reportCallIssue] -- pydantic-settings dunder kwarg


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
    assert thirty_two == settings.SECRET_KEY


# --- SEC-001: prod/debug cross-check + decoupled SQL echo ---


def test_production_with_debug_true_is_rejected():
    with pytest.raises(ValidationError) as excinfo:
        _build(ENVIRONMENT="production", DEBUG=True)
    msg = str(excinfo.value)
    assert "DEBUG" in msg
    assert "production" in msg


def test_production_with_debug_false_is_accepted():
    settings = _build(ENVIRONMENT="production", DEBUG=False)
    assert settings.ENVIRONMENT == "production"
    assert settings.DEBUG is False


def test_development_with_debug_true_is_accepted():
    settings = _build(ENVIRONMENT="development", DEBUG=True)
    assert settings.DEBUG is True


def test_sql_echo_inherits_debug_when_unset():
    assert _build(DEBUG=True).sql_echo is True
    assert _build(DEBUG=False).sql_echo is False


def test_sql_echo_explicit_overrides_debug():
    # Decoupled: SQL echo can be forced off even with DEBUG on (and vice-versa).
    assert _build(DEBUG=True, SQL_ECHO=False).sql_echo is False
    assert _build(ENVIRONMENT="development", DEBUG=False, SQL_ECHO=True).sql_echo is True


# --- SEC-005: prod + memory:// rate-limit storage warning ---
#
# These capture the validator's warning by stubbing ``config.logger.warning``
# directly rather than via ``caplog`` — caplog depends on logger propagation /
# global logging state, which other tests in the full suite reconfigure (the
# warning fired but wasn't captured in CI). Stubbing the method is deterministic.


@pytest.fixture
def captured_warnings(monkeypatch):
    """Capture messages passed to ``app.core.config.logger.warning``."""
    from app.core import config as config_mod

    msgs: list[str] = []
    monkeypatch.setattr(config_mod.logger, "warning", lambda msg, *a, **k: msgs.append(msg))
    return msgs


def test_production_with_memory_rate_limit_warns(captured_warnings):
    # Per-process memory:// buckets don't hold across multiple workers in prod.
    _build(ENVIRONMENT="production", DEBUG=False, RATE_LIMIT_STORAGE_URL="memory://")
    assert any("memory://" in m for m in captured_warnings)


def test_production_with_redis_rate_limit_does_not_warn(captured_warnings):
    _build(
        ENVIRONMENT="production",
        DEBUG=False,
        RATE_LIMIT_STORAGE_URL="redis://localhost:6379/0",
    )
    assert not any("memory://" in m for m in captured_warnings)


def test_development_with_memory_rate_limit_does_not_warn(captured_warnings):
    _build(ENVIRONMENT="development", RATE_LIMIT_STORAGE_URL="memory://")
    assert not any("memory://" in m for m in captured_warnings)
