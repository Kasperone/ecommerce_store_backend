"""Unit tests for ``app.crud.user.CRUDUser``.

These tests touch the database (via the per-test savepoint session) but do
not go through HTTP — they cover the state machine that the auth route
delegates to. They are marked ``integration`` because they need Postgres.
"""

from datetime import timedelta

import pytest

from app.core.security import verify_password
from app.crud import user as crud_user
from app.crud.user import (
    LOGIN_LOCKOUT_DURATION,
    LOGIN_LOCKOUT_THRESHOLD,
    _utcnow_naive,
)
from tests.factories.user import DEFAULT_PASSWORD


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# authenticate
# ---------------------------------------------------------------------------


def test_authenticate_happy_path(db_session, user_factory):
    user = user_factory(email="auth-ok@example.com")
    got = crud_user.authenticate(
        db_session, email="auth-ok@example.com", password=DEFAULT_PASSWORD
    )
    assert got is not None
    assert got.id == user.id


def test_authenticate_returns_none_for_unknown_email(db_session):
    assert (
        crud_user.authenticate(
            db_session, email="ghost@example.com", password=DEFAULT_PASSWORD
        )
        is None
    )


def test_authenticate_returns_none_for_wrong_password(db_session, user_factory):
    user_factory(email="auth-bad@example.com")
    assert (
        crud_user.authenticate(
            db_session, email="auth-bad@example.com", password="not-the-password"
        )
        is None
    )


# ---------------------------------------------------------------------------
# Lockout state machine
# ---------------------------------------------------------------------------


def test_register_failed_login_increments_counter(db_session, user_factory):
    user = user_factory(email="lock@example.com")
    just_locked = crud_user.register_failed_login(db_session, user=user)
    assert just_locked is False
    assert user.failed_login_attempts == 1
    assert user.locked_until is None


def test_register_failed_login_locks_at_threshold(db_session, user_factory):
    user = user_factory(email="lock-thresh@example.com")
    transitions = []
    for _ in range(LOGIN_LOCKOUT_THRESHOLD):
        transitions.append(crud_user.register_failed_login(db_session, user=user))
    # Only the threshold-crossing call returns True.
    assert transitions == [False] * (LOGIN_LOCKOUT_THRESHOLD - 1) + [True]
    assert user.failed_login_attempts == LOGIN_LOCKOUT_THRESHOLD
    assert user.locked_until is not None
    # Lock window is approximately LOGIN_LOCKOUT_DURATION ahead.
    delta = user.locked_until - _utcnow_naive()
    assert timedelta(seconds=0) < delta <= LOGIN_LOCKOUT_DURATION + timedelta(seconds=5)


def test_register_successful_login_clears_state(db_session, user_factory):
    user = user_factory(
        email="cleared@example.com",
        failed_login_attempts=4,
        locked_until=_utcnow_naive() - timedelta(minutes=1),
    )
    crud_user.register_successful_login(db_session, user=user)
    assert user.failed_login_attempts == 0
    assert user.locked_until is None


def test_register_successful_login_is_noop_when_already_clean(db_session, user_factory):
    user = user_factory(email="clean@example.com")
    assert user.failed_login_attempts == 0
    crud_user.register_successful_login(db_session, user=user)
    assert user.failed_login_attempts == 0
    assert user.locked_until is None


def test_is_locked_true_when_locked_until_in_future(db_session, user_factory):
    user = user_factory(
        email="locked-future@example.com",
        locked_until=_utcnow_naive() + timedelta(minutes=5),
    )
    assert crud_user.is_locked(user) is True


def test_is_locked_false_when_locked_until_in_past(db_session, user_factory):
    user = user_factory(
        email="locked-past@example.com",
        locked_until=_utcnow_naive() - timedelta(seconds=1),
    )
    assert crud_user.is_locked(user) is False


def test_is_locked_false_when_locked_until_is_null(db_session, user_factory):
    user = user_factory(email="never-locked@example.com")
    assert user.locked_until is None
    assert crud_user.is_locked(user) is False


# ---------------------------------------------------------------------------
# Password reset token lifecycle
# ---------------------------------------------------------------------------


def test_issue_then_consume_password_reset_token(db_session, user_factory):
    user = user_factory(email="reset@example.com")
    token = crud_user.issue_password_reset_token(db_session, user=user)

    assert user.password_reset_token_hash is not None
    assert user.password_reset_expires_at is not None

    before = user.password_changed_at
    ok, returned = crud_user.consume_password_reset_token(
        db_session, token=token, new_password="NewS3cret!Pass"
    )
    assert ok is True
    assert returned is not None
    assert returned.id == user.id
    # Token wiped, password rotated, change-marker bumped.
    assert returned.password_reset_token_hash is None
    assert returned.password_reset_expires_at is None
    assert verify_password("NewS3cret!Pass", returned.hashed_password)
    assert returned.password_changed_at >= before


def test_consume_password_reset_token_rejects_unknown_token(db_session, user_factory):
    user_factory(email="reset-bad@example.com")
    ok, returned = crud_user.consume_password_reset_token(
        db_session, token="totally-bogus", new_password="NewS3cret!Pass"
    )
    assert ok is False
    assert returned is None


def test_consume_password_reset_token_rejects_expired_token(db_session, user_factory):
    user = user_factory(email="reset-expired@example.com")
    token = crud_user.issue_password_reset_token(db_session, user=user)
    # Force expiry into the past.
    user.password_reset_expires_at = _utcnow_naive() - timedelta(seconds=1)
    db_session.add(user)
    db_session.commit()

    ok, returned = crud_user.consume_password_reset_token(
        db_session, token=token, new_password="NewS3cret!Pass"
    )
    assert ok is False
    assert returned is None


def test_consume_password_reset_token_clears_lockout(db_session, user_factory):
    user = user_factory(
        email="reset-locked@example.com",
        failed_login_attempts=LOGIN_LOCKOUT_THRESHOLD,
        locked_until=_utcnow_naive() + timedelta(minutes=5),
    )
    token = crud_user.issue_password_reset_token(db_session, user=user)
    ok, returned = crud_user.consume_password_reset_token(
        db_session, token=token, new_password="NewS3cret!Pass"
    )
    assert ok is True
    assert returned is not None
    assert returned.failed_login_attempts == 0
    assert returned.locked_until is None


# ---------------------------------------------------------------------------
# change_password
# ---------------------------------------------------------------------------


def test_change_password_requires_correct_current_password(db_session, user_factory):
    user = user_factory(email="change-bad@example.com")
    original_hash = user.hashed_password
    ok = crud_user.change_password(
        db_session,
        user=user,
        current_password="wrong-password",
        new_password="NewS3cret!Pass",
    )
    assert ok is False
    assert user.hashed_password == original_hash


def test_change_password_rotates_and_bumps_password_changed_at(db_session, user_factory):
    user = user_factory(email="change-ok@example.com")
    before = user.password_changed_at
    ok = crud_user.change_password(
        db_session,
        user=user,
        current_password=DEFAULT_PASSWORD,
        new_password="NewS3cret!Pass",
    )
    assert ok is True
    assert verify_password("NewS3cret!Pass", user.hashed_password)
    assert user.password_changed_at >= before


def test_change_password_clears_pending_reset_and_lockout(db_session, user_factory):
    user = user_factory(
        email="change-clears@example.com",
        failed_login_attempts=3,
        locked_until=_utcnow_naive() + timedelta(minutes=5),
    )
    crud_user.issue_password_reset_token(db_session, user=user)
    assert user.password_reset_token_hash is not None

    ok = crud_user.change_password(
        db_session,
        user=user,
        current_password=DEFAULT_PASSWORD,
        new_password="NewS3cret!Pass",
    )
    assert ok is True
    assert user.password_reset_token_hash is None
    assert user.password_reset_expires_at is None
    assert user.failed_login_attempts == 0
    assert user.locked_until is None
