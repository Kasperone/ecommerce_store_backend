"""Off-session-charge due net-terms orders for auto-charge companies (cron entry).

    python -m app.scripts.charge_net_terms_orders

No in-app scheduler — OS cron drives it. Daily is right (due dates have day
granularity), e.g.:

    41 9 * * * cd <backend> && .venv/bin/python -m app.scripts.charge_net_terms_orders

Exits 0 always — Stripe unconfigured, or no auto-charge companies, is a healthy
no-op; a declined card is handled best-effort inside the service (logged, order
left unpaid for dunning/manual, never fatal). The charge count is logged.
"""

import logging

from app.core.database import SessionLocal
from app.services.net_terms_autocharge import charge_due_orders

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("charge_net_terms_orders")


def main() -> None:
    db = SessionLocal()
    try:
        charged = charge_due_orders(db)
        logger.info("Done: %d net-terms order(s) auto-charged", charged)
    finally:
        db.close()


if __name__ == "__main__":
    main()
