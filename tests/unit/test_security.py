"""Unit tests for app.core.security — pure functions, no DB."""

from datetime import datetime, timedelta, timezone

import pytest
from jose import JWTError, jwt

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_password_hash,
    hash_refresh_token,
    verify_password,
)


# ---------------------------------------------------------------------------
# Argon2id password hashing
# ---------------------------------------------------------------------------


def test_hash_then_verify_roundtrip():
    h = get_password_hash("CorrectHorseBattery!9")
    assert h.startswith("$argon2id$"), "should use Argon2id, not bcrypt"
    assert verify_password("CorrectHorseBattery!9", h) is True


def test_verify_rejects_wrong_password():
    h = get_password_hash("CorrectHorseBattery!9")
    assert verify_password("CorrectHorseBattery!8", h) is False


def test_hash_is_salted_per_call():
    a = get_password_hash("same-input")
    b = get_password_hash("same-input")
    assert a != b
    assert verify_password("same-input", a)
    assert verify_password("same-input", b)


# ---------------------------------------------------------------------------
# JWT encode / decode
# ---------------------------------------------------------------------------


def test_access_token_roundtrip():
    token, jti, exp = create_access_token(subject=42)
    decoded = decode_token(token, expected_type="access")
    assert decoded.subject == "42"
    assert decoded.jti == jti
    assert decoded.type == "access"
    # exp returned by builder is tz-aware UTC; decoded is also tz-aware UTC.
    assert abs((decoded.expires_at - exp).total_seconds()) < 1


def test_refresh_token_roundtrip():
    token, jti, _ = create_refresh_token(subject="user-7")
    decoded = decode_token(token, expected_type="refresh")
    assert decoded.subject == "user-7"
    assert decoded.jti == jti
    assert decoded.type == "refresh"


def test_decode_rejects_wrong_type():
    """Access token presented to a refresh-type decode must fail."""
    token, _, _ = create_access_token(subject=1)
    with pytest.raises(JWTError):
        decode_token(token, expected_type="refresh")


def test_decode_rejects_tampered_signature():
    token, _, _ = create_access_token(subject=1)
    # Flip the last char of the signature segment.
    head, _, last = token.rpartition(".")
    bad_sig = "A" if last[-1] != "A" else "B"
    tampered = f"{head}.{last[:-1]}{bad_sig}"
    with pytest.raises(JWTError):
        decode_token(tampered, expected_type="access")


def test_decode_rejects_expired_token():
    token, _, _ = create_access_token(
        subject=1, expires_delta=timedelta(seconds=-1)
    )
    with pytest.raises(JWTError):
        decode_token(token, expected_type="access")


def test_decode_rejects_wrong_audience():
    """Token signed with a different audience must not validate against ours."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "1",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "iss": settings.JWT_ISSUER,
        "aud": "some-other-audience",
        "jti": "deadbeef",
        "type": "access",
    }
    bad_token = jwt.encode(
        payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
    )
    with pytest.raises(JWTError):
        decode_token(bad_token, expected_type="access")


def test_decode_rejects_missing_claims():
    """A signature-valid token without required claims is rejected."""
    now = datetime.now(timezone.utc)
    payload = {
        # Missing jti.
        "sub": "1",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "type": "access",
    }
    bad_token = jwt.encode(
        payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
    )
    with pytest.raises(JWTError):
        decode_token(bad_token, expected_type="access")


def test_each_token_carries_a_unique_jti():
    _, jti1, _ = create_access_token(subject=1)
    _, jti2, _ = create_access_token(subject=1)
    assert jti1 != jti2


# ---------------------------------------------------------------------------
# Refresh-token hashing
# ---------------------------------------------------------------------------


def test_hash_refresh_token_is_deterministic():
    assert hash_refresh_token("abc") == hash_refresh_token("abc")


def test_hash_refresh_token_differs_per_input():
    assert hash_refresh_token("abc") != hash_refresh_token("abd")


def test_hash_refresh_token_is_sha256_hex():
    digest = hash_refresh_token("any-token-value")
    assert len(digest) == 64
    int(digest, 16)  # raises if not hex
