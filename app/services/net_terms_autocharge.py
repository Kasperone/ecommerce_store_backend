"""B2B net-terms auto-charge at term end (opt-in per company, tail Program C).

For companies that opted into auto-pay (``auto_charge_enabled``) and vaulted a
card (``stripe_customer_id`` + ``auto_charge_payment_method_id`` via the portal
SetupIntent flow), this one-shot pass off-session-charges the vaulted card once a
net-terms order reaches its due date — instead of waiting for a manual bank
transfer (which stays the default for every other net-terms company).

No scheduler in this app: an OS-cron entry point
(``app.scripts.charge_net_terms_orders``) drives it. The off-session charge is
**synchronous** (``confirm=True``), so success settles the order inline — no
webhook needed. Payment is tracked ORTHOGONALLY to fulfilment (Program 2): a
charged order stamps ``net_terms_paid_at`` and NEVER enters the ``PAID`` status.

The charge is for ``order.amount_due`` (total minus gift-card / store-credit
tender), never the raw ``total`` — an order already covered by stored value is
settled inline with no Stripe call at all.

Best-effort per order + commit per success, so a crash never double-charges. A
declined card leaves the order unpaid (dunning / manual transfer still apply) and
records a ``net_terms_auto_charge_failed`` order_event. Idempotency-keyed on the
order id, so a retried run is a Stripe no-op even mid-crash.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from app.core.money import quantize_money
from app.crud import order_event as crud_order_event
from app.models.company import Company
from app.models.order import Order, OrderStatus
from app.services.b2b_net_terms import NET_TERMS_METHOD
from app.services.stripe import StripeError, stripe_configured

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class ChargeService(Protocol):
    """The one Stripe capability this pass needs — lets tests inject a fake."""

    def charge_off_session(
        self,
        *,
        customer_id: str,
        payment_method_id: str,
        amount_minor: int,
        currency: str,
        ext_order_id: str,
        idempotency_key: str,
    ) -> dict: ...


logger = logging.getLogger(__name__)

CHARGED_EVENT_KIND = "net_terms_auto_charged"
FAILED_EVENT_KIND = "net_terms_auto_charge_failed"


def _amount_minor(amount: Decimal) -> int:
    """Money amount → integer smallest unit (cents/grosze; USD/PLN/EUR are 2-decimal)."""
    return int(quantize_money(amount) * 100)


def charge_due_orders(db: Session, *, service: ChargeService | None = None) -> int:
    """Off-session-charge every due, unpaid net-terms order of an auto-charge company.

    Returns the number of orders successfully charged (paid). No-op (0) when Stripe
    is unconfigured. ``service`` is injectable for tests; otherwise built from
    settings.
    """
    if service is None:
        if not stripe_configured():
            logger.info("Stripe not configured; net-terms auto-charge is a no-op")
            return 0
        from app.services.stripe import get_stripe_service

        service = get_stripe_service()

    now = datetime.now(UTC).replace(tzinfo=None)
    due = (
        db.query(Order)
        .join(Company, Order.company_id == Company.id)
        .filter(
            Order.payment_method == NET_TERMS_METHOD,
            Order.net_terms_paid_at.is_(None),
            Order.status != OrderStatus.CANCELLED,
            Order.payment_due_at.isnot(None),
            Order.payment_due_at <= now,
            Company.auto_charge_enabled.is_(True),
            Company.stripe_customer_id.isnot(None),
            Company.auto_charge_payment_method_id.isnot(None),
        )
        .all()
    )

    charged = 0
    for order in due:
        company = order.company

        # Stored value (gift card / store credit) is TENDER, not a discount: it
        # leaves ``total`` untouched and lowers ``amount_due``. Charging ``total``
        # here would silently bill the buyer twice for the covered part.
        if order.amount_due <= 0:
            # Fully covered at checkout — nothing left to collect, and Stripe
            # rejects a zero/negative charge. Settle it without touching Stripe.
            order.net_terms_paid_at = now
            db.add(order)
            crud_order_event.record(
                db,
                order_id=order.id,
                kind=CHARGED_EVENT_KIND,
                payload={"reason": "covered_by_stored_value"},
            )
            db.commit()
            charged += 1
            continue

        try:
            result = service.charge_off_session(
                customer_id=company.stripe_customer_id,  # type: ignore[arg-type]  -- filtered non-null
                payment_method_id=company.auto_charge_payment_method_id,  # type: ignore[arg-type]
                amount_minor=_amount_minor(order.amount_due),
                currency=order.currency,
                ext_order_id=order.order_number,
                idempotency_key=f"net-terms-autocharge-{order.id}",
            )
        except StripeError:
            logger.warning(
                "Net-terms auto-charge declined/failed: order=%s", order.order_number, exc_info=True
            )
            crud_order_event.record(
                db, order_id=order.id, kind=FAILED_EVENT_KIND, payload={"reason": "charge_failed"}
            )
            db.commit()
            continue

        if result.get("status") != "succeeded":
            # requires_action / processing — not settled synchronously. Leave unpaid;
            # record the attempt so a retry (or manual settlement) has the trail.
            logger.info(
                "Net-terms auto-charge not settled (status=%s): order=%s",
                result.get("status"),
                order.order_number,
            )
            crud_order_event.record(
                db,
                order_id=order.id,
                kind=FAILED_EVENT_KIND,
                payload={"reason": "not_settled", "status": result.get("status")},
            )
            db.commit()
            continue

        order.net_terms_paid_at = now
        db.add(order)
        crud_order_event.record(
            db,
            order_id=order.id,
            kind=CHARGED_EVENT_KIND,
            payload={"payment_intent_id": result.get("id")},
        )
        db.commit()
        charged += 1

    logger.info("Net-terms auto-charge run: %d due, %d charged", len(due), charged)
    return charged
