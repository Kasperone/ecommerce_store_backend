"""Shared pytest fixtures.

Load order matters: ``.env.test`` must be applied to ``os.environ`` BEFORE any
``app.*`` module is imported, because ``app.core.config.settings`` is built
once at import time and reads from environment.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# Load .env.test if present (local dev). In CI, env vars are pre-set by the
# workflow and the file does not exist.
_env_test = BACKEND_ROOT / ".env.test"
if _env_test.exists():
    load_dotenv(_env_test, override=True)

# Hard guard: refuse to run if DATABASE_URL still points at a non-test name.
# Catches the case of a developer running pytest without .env.test in place.
_db_url = os.environ.get("DATABASE_URL", "")
if "test" not in _db_url.lower():
    raise RuntimeError(
        "Refusing to run tests against a non-test database. "
        f"DATABASE_URL={_db_url!r} must contain 'test' (e.g. ecommerce_test). "
        "Copy .env.test.example to .env.test and adjust."
    )

# Now safe to import app code.
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.dependencies import get_db  # noqa: E402
from app.main import app  # noqa: E402

# ---------------------------------------------------------------------------
# Database lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def db_engine():
    """Build a Postgres engine against the test DB and run alembic migrations.

    Drops the public schema first so re-runs always start from an empty DB
    and migrations apply against a clean slate.
    """
    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()

    alembic_cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    # alembic env.py reads settings.DATABASE_URL on its own — no extra wiring.
    command.upgrade(alembic_cfg, "head")

    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine) -> Iterator[Session]:
    """Per-test SQLAlchemy session with savepoint-based rollback.

    ``join_transaction_mode="create_savepoint"`` lets CRUD code call
    ``session.commit()`` (which it does — see app/crud/*) while keeping the
    outer transaction open. The outer rollback at teardown undoes everything.
    """
    connection = db_engine.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(
        bind=connection,
        autoflush=False,
        autocommit=False,
        join_transaction_mode="create_savepoint",
    )
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# FastAPI test client
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(db_session: Session) -> AsyncIterator[AsyncClient]:
    """httpx AsyncClient bound to the FastAPI app via ASGI transport.

    Overrides ``get_db`` so route handlers see ``db_session`` and uncommitted
    writes from the test are visible to the API.
    """

    def _get_test_db():
        try:
            yield db_session
        finally:
            # Don't close the session here — the db_session fixture owns the
            # lifecycle. Closing twice causes "Session is already closed".
            pass

    app.dependency_overrides[get_db] = _get_test_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Email mocking — no test should ever hit the Resend API.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def captured_emails(monkeypatch) -> list[dict]:
    """Capture every send_* call into a list; assert on it from tests.

    Each entry: ``{"kind": "verification|welcome|password_reset|...",
    "email_to": str, "username": str, "token": str | None}``.
    """
    captured: list[dict] = []

    def _make(kind: str):
        def _fn(*, email_to: str, username: str | None = None, **kwargs):
            captured.append({"kind": kind, "email_to": email_to, "username": username, **kwargs})
            return {"id": f"mock-{kind}"}

        return _fn

    import app.core.email as email_mod

    monkeypatch.setattr(email_mod, "send_email", _make("generic"))
    monkeypatch.setattr(email_mod, "send_verification_email", _make("verification"))
    monkeypatch.setattr(email_mod, "send_welcome_email", _make("welcome"))
    monkeypatch.setattr(email_mod, "send_password_reset_email", _make("password_reset"))
    monkeypatch.setattr(email_mod, "send_account_locked_email", _make("account_locked"))
    monkeypatch.setattr(email_mod, "send_already_registered_email", _make("already_registered"))

    # Also patch the names re-imported into auth.py at module load time.
    import app.api.v1.auth as auth_mod

    for name in (
        "send_verification_email",
        "send_welcome_email",
        "send_password_reset_email",
        "send_account_locked_email",
        "send_already_registered_email",
    ):
        if hasattr(auth_mod, name):
            monkeypatch.setattr(
                auth_mod, name, _make(name.replace("send_", "").replace("_email", ""))
            )

    return captured


# ---------------------------------------------------------------------------
# Rate-limiter reset
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_limiter():
    """Reset the slowapi in-memory limiter so per-IP/per-email counters from
    earlier tests do not bleed into this one. Use on tests marked
    ``@pytest.mark.rate_limit`` (or any test where rate limits matter)."""
    from app.core.rate_limit import limiter

    limiter.reset()
    yield
    limiter.reset()


# ---------------------------------------------------------------------------
# User factory fixture (factory-boy backed)
# ---------------------------------------------------------------------------


@pytest.fixture
def user_factory(db_session):
    """Returns a callable that creates a User row via the factory.

    Usage::

        def test_thing(user_factory):
            u = user_factory(is_verified=True)
    """
    from tests.factories.user import UserFactory

    UserFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- factory-boy SQLAlchemyOptions attributes aren't surfaced on the FactoryOptions stub
    UserFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue] -- factory-boy SQLAlchemyOptions attributes aren't surfaced on the FactoryOptions stub

    def _make(**kwargs):
        return UserFactory(**kwargs)

    return _make


# ---------------------------------------------------------------------------
# Catalog factory fixtures (Category, Product)
# ---------------------------------------------------------------------------


@pytest.fixture
def category_factory(db_session):
    """Returns a callable that creates a Category row via the factory."""
    from tests.factories.category import CategoryFactory

    CategoryFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- see user_factory
    CategoryFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue] -- see user_factory

    def _make(**kwargs):
        return CategoryFactory(**kwargs)

    return _make


@pytest.fixture
def product_factory(db_session):
    """Returns a callable that creates a Product row via the factory.

    Internally uses ``CategoryFactory`` as a SubFactory, which needs the
    same session binding.
    """
    from tests.factories.category import CategoryFactory
    from tests.factories.product import ProductFactory

    CategoryFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- see user_factory
    CategoryFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue] -- see user_factory
    ProductFactory._meta.sqlalchemy_session = db_session  # pyright: ignore[reportAttributeAccessIssue] -- see user_factory
    ProductFactory._meta.sqlalchemy_session_persistence = "commit"  # pyright: ignore[reportAttributeAccessIssue] -- see user_factory

    def _make(**kwargs):
        return ProductFactory(**kwargs)

    return _make
