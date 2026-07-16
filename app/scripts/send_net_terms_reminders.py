"""Send B2B net-terms overdue reminders (external-cron entry point).

    python -m app.scripts.send_net_terms_reminders

There is no scheduler in this app, so OS cron drives detection. Daily is plenty
(due dates have day granularity), e.g.:

    23 8 * * * cd <backend> && .venv/bin/python -m app.scripts.send_net_terms_reminders

Exits 0 always — a disabled feature (``settings.NET_TERMS_DUNNING_ENABLED``
false) is a healthy no-op, and per-order send failures are handled best-effort
inside the service (logged, skipped, never fatal). The send count is logged.
"""

import logging

from app.core.database import SessionLocal
from app.services.net_terms_dunning import send_net_terms_reminders

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("send_net_terms_reminders")


def main() -> None:
    db = SessionLocal()
    try:
        sent = send_net_terms_reminders(db)
        logger.info("Done: %d net-terms reminder(s) sent", sent)
    finally:
        db.close()


if __name__ == "__main__":
    main()
