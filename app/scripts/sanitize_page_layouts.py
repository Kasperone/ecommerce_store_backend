"""Re-sanitize every stored CMS page layout (one-off backfill for SEC-012).

    python -m app.scripts.sanitize_page_layouts          # apply
    python -m app.scripts.sanitize_page_layouts --dry-run  # report only

Before SEC-012, only the ``custom-html`` block's HTML was sanitized on save;
``rich-text``/``image-with-text`` bodies and ``faq`` answers were stored raw and
rendered via ``v-html`` on public pages. The route now sanitizes all of them on
create/save/publish, but layouts written *before* the fix may still hold raw
HTML in ``draft_layout`` / ``published_layout``. This script runs the same nh3
allowlist (``sanitize_layout_blob``) over both columns of every page and commits
the cleaned result.

Idempotent: re-running on already-clean layouts is a no-op (nh3 is stable).
``page_versions`` snapshots are intentionally left untouched (immutable history;
they only re-enter the live path via rollback → re-publish, which re-sanitizes).

Exits non-zero on error so an operator run surfaces problems loudly.
"""

import argparse
import copy
import logging
import sys

from app.api.v1.admin_pages import sanitize_layout_blob
from app.core.database import SessionLocal
from app.models.page import Page

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("sanitize_page_layouts")


def _sanitized(blob: object) -> dict | None:
    """Return a sanitized deep copy if ``blob`` is a layout dict, else ``None``."""
    if not isinstance(blob, dict):
        return None
    return sanitize_layout_blob(copy.deepcopy(blob))


def sanitize_all(*, dry_run: bool) -> int:
    """Re-sanitize draft_layout + published_layout on every page. Returns the
    number of pages whose stored layout changed."""
    db = SessionLocal()
    changed = 0
    try:
        pages = db.query(Page).all()
        logger.info("Scanning %d page(s)", len(pages))
        for page in pages:
            page_changed = False
            for column in ("draft_layout", "published_layout"):
                current = getattr(page, column)
                cleaned = _sanitized(current)
                if cleaned is not None and cleaned != current:
                    logger.info(
                        "page id=%s slug=%s: %s has HTML that changed on sanitize",
                        page.id,
                        page.slug,
                        column,
                    )
                    if not dry_run:
                        setattr(page, column, cleaned)
                    page_changed = True
            if page_changed:
                changed += 1
        if dry_run:
            logger.info("Dry run: %d page(s) would change (no writes)", changed)
            db.rollback()
        else:
            db.commit()
            logger.info("Committed: %d page(s) re-sanitized", changed)
        return changed
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report which pages would change without writing.",
    )
    args = parser.parse_args()
    try:
        sanitize_all(dry_run=args.dry_run)
    except Exception:
        logger.exception("Backfill failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
