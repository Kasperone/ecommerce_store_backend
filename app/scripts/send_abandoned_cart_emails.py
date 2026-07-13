"""Send abandoned-cart recovery emails (external-cron entry point).

    python -m app.scripts.send_abandoned_cart_emails

There is no scheduler in this app, so OS cron drives detection, e.g. hourly
(threshold granularity is hours):

    17 * * * * cd <backend> && .venv/bin/python -m app.scripts.send_abandoned_cart_emails

Exits 0 always — a disabled feature (``store_config.abandoned_cart_enabled``
false) is a healthy no-op, and per-user send failures are already handled
best-effort inside the service (logged, skipped, never fatal). The send count
is logged.
"""

import logging

from app.core.database import SessionLocal
from app.services.abandoned_cart_email import send_abandoned_cart_emails

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("send_abandoned_cart_emails")


def main() -> None:
    db = SessionLocal()
    try:
        sent = send_abandoned_cart_emails(db)
        logger.info("Done: %d abandoned-cart email(s) sent", sent)
    finally:
        db.close()


if __name__ == "__main__":
    main()
