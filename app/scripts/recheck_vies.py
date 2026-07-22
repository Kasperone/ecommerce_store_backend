"""Re-validate ageing VIES VAT-ID confirmations (external-cron entry point).

    python -m app.scripts.recheck_vies

There is no scheduler in this app, so OS cron drives the re-check. Daily is
plenty (confirmations age in days), e.g.:

    41 6 * * * cd <backend> && .venv/bin/python -m app.scripts.recheck_vies

Exits 0 always — a disabled feature (``settings.VIES_ENABLED`` false) is a
healthy no-op, and per-company VIES outages are handled inside the service
(attempt recorded, skipped, retried next run). Never on the checkout path.
"""

import logging

from app.core.database import SessionLocal
from app.services.vies import recheck_stale_confirmations

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("recheck_vies")


def main() -> None:
    db = SessionLocal()
    try:
        count = recheck_stale_confirmations(db)
        logger.info("Done: re-checked %d VIES confirmation(s)", count)
    finally:
        db.close()


if __name__ == "__main__":
    main()
