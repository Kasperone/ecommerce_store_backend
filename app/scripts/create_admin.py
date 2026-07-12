"""Create a pre-verified ADMIN user non-interactively (fresh-install bootstrap).

    ADMIN_PASSWORD='...' python -m app.scripts.create_admin --email admin@example.com
    # or pipe the password on stdin:
    printf '%s' "$PW" | python -m app.scripts.create_admin --email admin@example.com

The password is NEVER accepted via argv (it would leak into shell history and
``ps``). Sources, in order: the ``ADMIN_PASSWORD`` env var, else the first line
of stdin when piped. If neither is available the script exits 2 with a usage
message. The password is validated against the same policy as registration
(8-100 chars, at least one digit + one uppercase) and hashed with the project's
Argon2id helper.

Idempotent: if a user with the email already exists the script logs
``already exists (role=...)`` and exits 0 WITHOUT modifying it — no silent
password overwrite. Pass ``--update-password`` to explicitly rotate an existing
ADMIN's password (refuses to touch a non-admin account); rotation bumps
``password_changed_at`` and revokes refresh tokens, exactly like the
authenticated change-password flow.

Exit codes: 0 ok/noop, 1 unexpected error, 2 usage/validation error.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from pydantic import EmailStr as EmailStrType
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.security import get_password_hash
from app.crud.refresh_token import refresh_token as crud_refresh_token
from app.crud.user import _password_changed_bump
from app.crud.user import user as crud_user
from app.models.user import User, UserRole
from app.schemas.auth import _validate_password_strength

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("create_admin")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

# Mirrors the Field(min_length=8, max_length=100) bounds on the registration
# schemas (app/schemas/user.py + app/schemas/auth.py).
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 100

_email_adapter: TypeAdapter[str] = TypeAdapter(EmailStrType)


def validate_password(password: str) -> str | None:
    """Return an error message if ``password`` fails the registration policy,
    else ``None``. Same rules as ``UserCreate``: length bounds + the shared
    strength validator (digit + uppercase)."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"password must be at least {PASSWORD_MIN_LENGTH} characters"
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"password must be at most {PASSWORD_MAX_LENGTH} characters"
    try:
        _validate_password_strength(password)
    except ValueError as exc:
        return str(exc)
    return None


def resolve_password() -> tuple[str | None, str]:
    """Resolve the password from ``ADMIN_PASSWORD`` env, else piped stdin.

    Returns ``(password, "")`` on success or ``(None, error_message)``.
    Never reads argv — see module docstring.
    """
    env_password = os.environ.get("ADMIN_PASSWORD")
    if env_password:
        return env_password, ""
    if not sys.stdin.isatty():
        piped = sys.stdin.readline().rstrip("\r\n")
        if piped:
            return piped, ""
    return None, (
        "no password provided — set the ADMIN_PASSWORD env var or pipe the "
        "password on stdin (it is never accepted as a CLI argument)"
    )


def split_name(name: str) -> tuple[str, str]:
    """Split a display name into (first_name, last_name) for the User columns."""
    parts = name.strip().split(None, 1)
    first = parts[0] if parts else "Store"
    last = parts[1] if len(parts) > 1 else "Admin"
    return first[:100], last[:100]


def create_admin(
    db: Session,
    *,
    email: str,
    password: str,
    first_name: str,
    last_name: str,
    update_password: bool = False,
) -> str:
    """Create (or, with ``update_password``, rotate) the admin. Returns one of
    ``"created"`` / ``"exists"`` / ``"updated"``.

    Raises ``ValueError`` when asked to rotate the password of an existing
    non-admin account — that is an operator mistake, not something to do
    silently.
    """
    existing = crud_user.get_by_email(db, email=email)
    if existing is not None:
        if not update_password:
            logger.info(
                "user %s already exists (role=%s) — left untouched "
                "(pass --update-password to rotate)",
                email,
                existing.role.value,
            )
            return "exists"
        if existing.role != UserRole.ADMIN:
            raise ValueError(
                f"refusing to rotate password: {email} exists with "
                f"role={existing.role.value}, not ADMIN"
            )
        existing.hashed_password = get_password_hash(password)
        existing.password_changed_at = _password_changed_bump()
        db.add(existing)
        # Rotation invalidates every live session, same as change-password.
        crud_refresh_token.revoke_all_for_user(db, user_id=existing.id)
        db.commit()
        logger.info("password rotated for existing admin %s (sessions revoked)", email)
        return "updated"

    user = User(
        email=email,
        hashed_password=get_password_hash(password),
        first_name=first_name,
        last_name=last_name,
        role=UserRole.ADMIN,
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("created admin id=%s email=%s (verified)", user.id, user.email)
    return "created"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.create_admin",
        description="Create a pre-verified ADMIN user (password via ADMIN_PASSWORD env or stdin).",
    )
    parser.add_argument("--email", required=True, help="Admin email address")
    parser.add_argument(
        "--name", default="Store Admin", help='Display name, e.g. "Jane Doe" (default: Store Admin)'
    )
    parser.add_argument(
        "--update-password",
        action="store_true",
        help="Explicitly rotate the password if the admin already exists",
    )
    args = parser.parse_args(argv)

    try:
        email = _email_adapter.validate_python(args.email)
    except ValidationError:
        logger.error("invalid email address: %s", args.email)
        return EXIT_USAGE

    password, err = resolve_password()
    if password is None:
        logger.error("%s", err)
        return EXIT_USAGE
    policy_error = validate_password(password)
    if policy_error is not None:
        logger.error("password rejected: %s", policy_error)
        return EXIT_USAGE

    first_name, last_name = split_name(args.name)
    db = SessionLocal()
    try:
        create_admin(
            db,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            update_password=args.update_password,
        )
        return EXIT_OK
    except ValueError as exc:
        db.rollback()
        logger.error("%s", exc)
        return EXIT_USAGE
    except Exception:
        db.rollback()
        logger.exception("create_admin failed")
        return EXIT_ERROR
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
