"""Shared pytest fixtures.

Load order matters: ``.env.test`` must be applied to ``os.environ`` BEFORE any
``app.*`` module is imported, because ``app.core.config.settings`` is built
once at import time and reads from environment.
"""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# Load .env.test if present (local dev). In CI, env vars are pre-set by the
# workflow and the file does not exist.
#
# override=False so an explicit `DATABASE_URL=... pytest` wins over the file
# and works as an escape hatch. The hard guard below still refuses any URL
# without "test" in it, so this cannot point the suite at dev/prod.
_env_test = BACKEND_ROOT / ".env.test"
if _env_test.exists():
    load_dotenv(_env_test, override=False)

# Hard guard: refuse to run if DATABASE_URL still points at a non-test name.
# Catches the case of a developer running pytest without .env.test in place.
_db_url = os.environ.get("DATABASE_URL", "")
if "test" not in _db_url.lower():
    raise RuntimeError(
        "Refusing to run tests against a non-test database. "
        f"DATABASE_URL={_db_url!r} must contain 'test' (e.g. ecommerce_test). "
        "Copy .env.test.example to .env.test and adjust."
    )


def _preflight_db_reachable(db_url: str) -> None:
    """TCP-probe the database host before the suite imports app code.

    Without this, an unreachable host (laptop on a different network, tunnel
    down) surfaces as ~25 fixture errors with tracebacks that look like a DB
    or code fault. Fail once, fast, with the actual remedy instead.
    """
    parsed = urlsplit(db_url)
    host, port = parsed.hostname, parsed.port or 5432
    if host is None or host in ("localhost", "127.0.0.1", "::1"):
        return  # local: connection refused is already an unambiguous error

    try:
        with socket.create_connection((host, port), timeout=5):
            return
    except OSError as exc:
        raise RuntimeError(
            f"hive unreachable at {host}:{port} ({exc}).\n"
            "On a different network, or is the tunnel down?\n"
            "  Check:   ip -br addr | grep wg-hive\n"
            "  Bring up: nmcli con up wg-hive\n"
            "hive is addressed at its WireGuard IP (10.0.0.1) from every "
            "network; the LAN IP 192.168.1.14 only resolves at home and is no "
            "longer used in config. See .claude/notes/plans/infra/."
        ) from exc


_preflight_db_reachable(_db_url)

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
    and migrations apply against a clean slate. Then seeds a "Test Shipping"
    zone covering every ISO-2 country code referenced by the test suite + a
    free "test" method with rates in USD/PLN/EUR, so any test that creates
    an order can pass ``shipping_method_id`` and get a $0.00 shipping line.
    The seed is session-scoped (committed at the engine level) so per-test
    savepoint rollback doesn't undo it.
    """
    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.commit()

    alembic_cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    # alembic env.py reads settings.DATABASE_URL on its own — no extra wiring.
    command.upgrade(alembic_cfg, "head")

    # Session-scoped shipping seed (see fixture docstring above). Imports are
    # deferred so this fixture stays callable even before app code is ready
    # (migration step above creates the tables this writes to).
    from app.models.shipping import (
        ShippingMethod,
        ShippingRate,
        ShippingZone,
        ShippingZoneCountry,
    )

    SeedSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    seed_db = SeedSession()
    try:
        # Idempotent — Postgres' DROP SCHEMA above already wiped tables on a
        # fresh run, but a partial run (engine reused via --reuse-db) needs
        # the get-or-create dance.
        zone = seed_db.query(ShippingZone).filter(ShippingZone.name == "Test Zone").first()
        if zone is None:
            zone = ShippingZone(name="Test Zone", description="Seeded for tests", is_active=True)
            seed_db.add(zone)
            seed_db.flush()
            # Every ISO-2 code any test passes as shipping_country. Adding here
            # is cheap — better than wiring per-test fixtures for each new code.
            for code in ("PL", "DE", "FR", "US", "GB"):
                seed_db.add(ShippingZoneCountry(zone_id=zone.id, country_code=code))
            seed_db.flush()
        method = seed_db.query(ShippingMethod).filter(ShippingMethod.code == "test").first()
        if method is None:
            method = ShippingMethod(name="Test Shipping", code="test", is_active=True, sort_order=0)
            seed_db.add(method)
            seed_db.flush()
        # One rate per currency so tests can use any of USD/PLN/EUR. Amount = 0
        # keeps `order.total == subtotal` for the pre-shipping assertion shape
        # most existing tests use.
        for currency in ("USD", "PLN", "EUR"):
            exists = (
                seed_db.query(ShippingRate)
                .filter(
                    ShippingRate.zone_id == zone.id,
                    ShippingRate.method_id == method.id,
                    ShippingRate.currency == currency,
                )
                .first()
            )
            if exists is None:
                seed_db.add(
                    ShippingRate(
                        zone_id=zone.id,
                        method_id=method.id,
                        currency=currency,
                        amount=0.0,
                    )
                )
        seed_db.commit()
    finally:
        seed_db.close()

    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def test_shipping_method_id(db_engine) -> int:
    """Return the seeded "test" shipping method's id (session-cached).

    Tests that build order-create bodies pass this as ``shipping_method_id``;
    the seeded rate is $0.00 across USD/PLN/EUR so order totals stay equal to
    subtotal unless a test deliberately sets up a non-zero rate.
    """
    from app.models.shipping import ShippingMethod

    Session_ = sessionmaker(bind=db_engine)
    s = Session_()
    try:
        method = s.query(ShippingMethod).filter(ShippingMethod.code == "test").first()
        assert method is not None, "Test shipping method not seeded"
        return method.id
    finally:
        s.close()


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
    monkeypatch.setattr(email_mod, "send_order_email", _make("order_email"))
    monkeypatch.setattr(email_mod, "send_company_invite_email", _make("company_invite"))

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
