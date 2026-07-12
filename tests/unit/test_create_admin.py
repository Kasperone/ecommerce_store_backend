"""Tests for the non-interactive admin bootstrap (app/scripts/create_admin.py).

DB-backed coverage of the core ``create_admin`` function (create / noop /
explicit rotation) plus pure CLI-boundary tests for the password-source and
password-policy refusals (exit 2, no DB touched).
"""

from __future__ import annotations

import sys

import pytest

from app.core.security import get_password_hash, verify_password
from app.models.user import User, UserRole
from app.scripts.create_admin import (
    EXIT_USAGE,
    create_admin,
    main,
    resolve_password,
    split_name,
    validate_password,
)

EMAIL = "bootstrap-admin@example.com"
PASSWORD = "AdminBoot123"
NEW_PASSWORD = "RotatedBoot456"


def _user(db, email: str = EMAIL) -> User:
    return db.query(User).filter(User.email == email).one()


# ---------------------------------------------------------------------------
# Core function (DB-backed)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCreateAdmin:
    def test_creates_verified_admin(self, db_session):
        status = create_admin(
            db_session,
            email=EMAIL,
            password=PASSWORD,
            first_name="Store",
            last_name="Admin",
        )
        assert status == "created"
        user = _user(db_session)
        assert user.role == UserRole.ADMIN
        assert user.is_verified is True
        assert user.is_active is True
        assert verify_password(PASSWORD, user.hashed_password)

    def test_second_run_is_noop(self, db_session):
        create_admin(
            db_session, email=EMAIL, password=PASSWORD, first_name="Store", last_name="Admin"
        )
        status = create_admin(
            db_session,
            email=EMAIL,
            password=NEW_PASSWORD,  # different password must NOT be applied
            first_name="Other",
            last_name="Name",
        )
        assert status == "exists"
        assert db_session.query(User).filter(User.email == EMAIL).count() == 1
        user = _user(db_session)
        assert verify_password(PASSWORD, user.hashed_password)  # unchanged
        assert not verify_password(NEW_PASSWORD, user.hashed_password)
        assert user.first_name == "Store"  # profile untouched too

    def test_update_password_rotates(self, db_session):
        create_admin(
            db_session, email=EMAIL, password=PASSWORD, first_name="Store", last_name="Admin"
        )
        status = create_admin(
            db_session,
            email=EMAIL,
            password=NEW_PASSWORD,
            first_name="Store",
            last_name="Admin",
            update_password=True,
        )
        assert status == "updated"
        user = _user(db_session)
        assert verify_password(NEW_PASSWORD, user.hashed_password)
        assert not verify_password(PASSWORD, user.hashed_password)
        # Rotation invalidates in-flight access tokens.
        assert user.password_changed_at is not None

    def test_update_password_refuses_non_admin(self, db_session):
        db_session.add(
            User(
                email=EMAIL,
                hashed_password=get_password_hash(PASSWORD),
                first_name="Regular",
                last_name="Customer",
                role=UserRole.CUSTOMER,
                is_active=True,
                is_verified=True,
            )
        )
        db_session.commit()
        with pytest.raises(ValueError, match="not ADMIN"):
            create_admin(
                db_session,
                email=EMAIL,
                password=NEW_PASSWORD,
                first_name="X",
                last_name="Y",
                update_password=True,
            )
        user = _user(db_session)
        assert user.role == UserRole.CUSTOMER
        assert verify_password(PASSWORD, user.hashed_password)  # untouched


# ---------------------------------------------------------------------------
# CLI boundary — refusals happen before any DB connection
# ---------------------------------------------------------------------------


class TestCliRefusals:
    def test_refuses_without_env_or_stdin(self, monkeypatch):
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)  # no pipe
        assert main(["--email", EMAIL]) == EXIT_USAGE

    def test_refuses_weak_password_from_env(self, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", "short")
        assert main(["--email", EMAIL]) == EXIT_USAGE

    def test_refuses_invalid_email(self, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
        assert main(["--email", "not-an-email"]) == EXIT_USAGE

    def test_resolve_password_prefers_env(self, monkeypatch):
        monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
        password, err = resolve_password()
        assert password == PASSWORD
        assert err == ""

    def test_resolve_password_missing(self, monkeypatch):
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        password, err = resolve_password()
        assert password is None
        assert "ADMIN_PASSWORD" in err


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    @pytest.mark.parametrize(
        ("password", "ok"),
        [
            ("AdminBoot123", True),
            ("short1A", False),  # < 8 chars
            ("nouppercase123", False),  # no uppercase
            ("NoDigitsHere", False),  # no digit
            ("A1" + "x" * 99, False),  # > 100 chars
        ],
    )
    def test_validate_password_matches_registration_policy(self, password, ok):
        assert (validate_password(password) is None) is ok

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Store Admin", ("Store", "Admin")),
            ("Jane", ("Jane", "Admin")),
            ("Jane Anne Doe", ("Jane", "Anne Doe")),
            ("", ("Store", "Admin")),
        ],
    )
    def test_split_name(self, name, expected):
        assert split_name(name) == expected
