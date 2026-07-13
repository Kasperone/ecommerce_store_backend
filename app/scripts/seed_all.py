"""One-command fresh-install bootstrap: migrate + seed a brand-new store.

    ADMIN_EMAIL='admin@example.com' ADMIN_PASSWORD='...' \
        python -m app.scripts.seed_all

Runs every provisioning step a new client store needs, in order, each one
idempotent so the whole command is safe to re-run at any time:

  1. ``alembic upgrade head``          (skippable via ``--skip-migrations``)
  2. store_config singleton            (verify/create the id=1 row)
  3. shipping seed                     (zones/methods/rates — get-or-create)
  4. homepage CMS page seed            (published ``home`` page, en + pl)
  5. initial admin user                (only when ADMIN_EMAIL + ADMIN_PASSWORD
                                        env vars are set, else SKIP with hint)
  6. demo catalog                      (only with ``--demo``: browsable demo
                                        categories/products/variants/collection
                                        + a DEMO10 coupon, all ``demo-`` prefixed)
  7. Meilisearch reindex               (fail-SOFT: unconfigured/unreachable
                                        engine logs a warning and the exit
                                        code stays 0; ``--skip-search`` skips)

Without ``--demo`` the demo step is omitted entirely (5 seeding steps + the
reindex, numbered [n/6] exactly as before). The demo step runs *before* the
reindex so demo products land in the search indexes in the same bootstrap.

Every step other than the search reindex is fatal on failure (non-zero exit,
fail-fast) so scripted provisioning can't half-succeed silently.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

from app.core.database import SessionLocal
from app.core.search import meili_configured
from app.crud.store_config import SINGLETON_ID
from app.crud.store_config import store_config as crud_store_config
from app.models.store_config import StoreConfig
from app.scripts import reindex_search, seed_demo, seed_homepage_page, seed_shipping
from app.scripts.create_admin import main as create_admin_main

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("seed_all")

REPO_ROOT = Path(__file__).resolve().parents[2]

TOTAL_STEPS = 6


def step_migrations() -> None:
    """Run ``alembic upgrade head`` from the repo root. Raises on failure."""
    subprocess.run(  # noqa: S603 -- fixed argv, no user input
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        check=True,
    )


def step_store_config() -> str:
    """Ensure the store_config singleton row (id=1) exists.

    The ``add_store_config`` migration seeds it, and
    ``crud_store_config.get_singleton`` defensively creates it if a DB
    somehow lacks it — so this is a verify in practice and a create in the
    edge case. Returns ``"exists"`` or ``"created"``.
    """
    db = SessionLocal()
    try:
        if db.get(StoreConfig, SINGLETON_ID) is not None:
            return "exists"
        crud_store_config.get_singleton(db)  # stages the row (flush, no commit)
        db.commit()
        return "created"
    finally:
        db.close()


def step_shipping() -> None:
    """Baseline shipping zones/methods/rates (idempotent get-or-create)."""
    rc = seed_shipping.main()
    if rc != 0:
        raise RuntimeError(f"seed_shipping exited {rc}")


def step_homepage() -> None:
    """Published ``home`` CMS page for en + pl (idempotent per locale)."""
    rc = seed_homepage_page.main()
    if rc != 0:
        raise RuntimeError(f"seed_homepage_page exited {rc}")


def step_admin() -> str | None:
    """Create the initial admin when ADMIN_EMAIL + ADMIN_PASSWORD are set.

    Returns a skip-reason string when the env vars are absent, ``None`` when
    the admin step ran. Raises on a create_admin failure (fatal — a fresh
    install that asked for an admin must get one).
    """
    email = os.environ.get("ADMIN_EMAIL")
    password = os.environ.get("ADMIN_PASSWORD")
    if not email or not password:
        return (
            "ADMIN_EMAIL / ADMIN_PASSWORD not set — set both to create the "
            "initial admin (or run python -m app.scripts.create_admin later)"
        )
    argv = ["--email", email]
    name = os.environ.get("ADMIN_NAME")
    if name:
        argv += ["--name", name]
    rc = create_admin_main(argv)  # reads the password from ADMIN_PASSWORD env
    if rc != 0:
        raise RuntimeError(f"create_admin exited {rc}")
    return None


def step_demo() -> None:
    """Demo catalog (idempotent get-or-create on ``demo-`` slugs / DEMO10)."""
    rc = seed_demo.main()
    if rc != 0:
        raise RuntimeError(f"seed_demo exited {rc}")


def step_search() -> str | None:
    """Rebuild the Meilisearch indexes. Returns a skip-reason string when the
    engine is unconfigured; raises when configured but unreachable (the caller
    treats any failure here as non-fatal)."""
    if not meili_configured():
        return (
            "Meilisearch not configured (MEILISEARCH_URL / MEILISEARCH_MASTER_KEY) "
            "— run python -m app.scripts.reindex_search after configuring it"
        )
    reindex_search.reindex()
    return None


def run(args: argparse.Namespace) -> int:
    """Sequence the bootstrap steps. Fail-fast on any fatal step; the search
    reindex is the single fail-soft step (warn + exit 0)."""
    # ``--demo`` inserts one extra step before the reindex; without it the
    # numbering is exactly the historical [n/6].
    total = TOTAL_STEPS + (1 if args.demo else 0)

    def ok(n: int, name: str, detail: str = "") -> None:
        logger.info("[%d/%d] %s ... OK%s", n, total, name, f" ({detail})" if detail else "")

    def skip(n: int, name: str, reason: str) -> None:
        logger.info("[%d/%d] %s ... SKIP — %s", n, total, name, reason)

    def fail(n: int, name: str) -> int:
        logger.exception("[%d/%d] %s ... FAIL", n, total, name)
        return 1

    # 1. migrations
    if args.skip_migrations:
        skip(1, "alembic upgrade head", "--skip-migrations")
    else:
        try:
            step_migrations()
            ok(1, "alembic upgrade head")
        except Exception:
            return fail(1, "alembic upgrade head")

    # 2. store_config singleton
    try:
        ok(2, "store_config singleton", step_store_config())
    except Exception:
        return fail(2, "store_config singleton")

    # 3. shipping seed
    try:
        step_shipping()
        ok(3, "shipping seed")
    except Exception:
        return fail(3, "shipping seed")

    # 4. homepage page seed
    try:
        step_homepage()
        ok(4, "homepage page seed")
    except Exception:
        return fail(4, "homepage page seed")

    # 5. initial admin
    try:
        reason = step_admin()
        if reason is not None:
            skip(5, "initial admin", reason)
        else:
            ok(5, "initial admin")
    except Exception:
        return fail(5, "initial admin")

    # 6 (only with --demo). demo catalog — before the reindex so the demo
    # products land in the search indexes in the same bootstrap.
    if args.demo:
        try:
            step_demo()
            ok(6, "demo catalog")
        except Exception:
            return fail(6, "demo catalog")

    # Last step: search reindex — fail-soft
    if args.skip_search:
        skip(total, "search reindex", "--skip-search")
    else:
        try:
            reason = step_search()
            if reason is not None:
                skip(total, "search reindex", reason)
            else:
                ok(total, "search reindex")
        except Exception as exc:
            logger.warning(
                "[%d/%d] search reindex ... FAIL (non-fatal): %s — rerun "
                "python -m app.scripts.reindex_search once Meilisearch is up",
                total,
                total,
                exc,
            )

    logger.info("bootstrap complete")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.seed_all",
        description="Fresh-install bootstrap: migrate + seed a new store (idempotent).",
    )
    parser.add_argument(
        "--skip-migrations",
        action="store_true",
        help="Skip alembic upgrade head (schema already managed elsewhere)",
    )
    parser.add_argument(
        "--skip-search",
        action="store_true",
        help="Skip the Meilisearch reindex step",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help=(
            "Also seed a browsable demo catalog (demo- prefixed categories/"
            "products/variants/collection + a DEMO10 coupon)"
        ),
    )
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
