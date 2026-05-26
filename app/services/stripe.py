"""Stripe payment integration — Checkout Sessions, hosted-redirect flow.

Mirrors the PayU service (``app/services/payu.py``): create a Stripe Checkout
Session for one of our pending orders, return Stripe's hosted ``url`` for the
buyer, and after payment verify the ``Stripe-Signature`` on the webhook before
trusting it and flipping the order ``pending → paid``.

``get_stripe_service`` raises ``HTTPException(503)`` when Stripe is unconfigured
so dev/CI boot and every other endpoint works without Stripe keys (mirrors
``get_payu_client`` / ``get_r2_client``). Live behaviour is exercised against
Stripe test mode once keys are set; the unit/integration tests drive the SDK's
create call via monkeypatch and verify webhooks with real Stripe signatures.

``import stripe`` here resolves to the installed SDK (absolute import); this
module's own name is ``app.services.stripe``, so there is no shadowing.
"""

import stripe
from fastapi import HTTPException, status

from app.core.config import settings

# Webhook secret is required to trust notifications; secret key to call the API.
# The publishable key is only needed for browser-side Stripe.js (Elements), which
# this hosted-Checkout flow does not use, so it is not required here.
_STRIPE_REQUIRED_FIELDS = (
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
)


class StripeError(Exception):
    """A Stripe API call failed or returned an unexpected shape."""


def stripe_configured() -> bool:
    return all(getattr(settings, name) for name in _STRIPE_REQUIRED_FIELDS)


class StripeService:
    """Thin wrapper over the Stripe SDK for the Checkout-Session flow.

    One instance per request (built by ``get_stripe_service``). The secret key is
    passed per call rather than mutating the SDK's module-global ``stripe.api_key``
    — no shared mutable state, and it mirrors the per-request PayU client.

    The SDK calls are synchronous (blocking HTTP); callers run ``create_checkout_session``
    in a threadpool so the event loop is not blocked. ``construct_event`` is local
    crypto (no network), so it is safe to call inline.
    """

    def __init__(self, *, secret_key: str, webhook_secret: str) -> None:
        self.secret_key = secret_key
        self.webhook_secret = webhook_secret

    def create_checkout_session(
        self,
        *,
        ext_order_id: str,
        amount_minor: int,
        currency: str,
        description: str,
        buyer_email: str,
        success_url: str,
        cancel_url: str,
    ) -> dict:
        """Create a Stripe Checkout Session; returns ``{checkout_url, session_id}``.

        A single aggregate line item for the whole order total (Stripe charges
        exactly ``amount_minor``, so the charge can never drift from
        ``order.total`` through per-item rounding). ``amount_minor`` is the integer
        smallest currency unit (cents/grosze) — USD/PLN/EUR are all 2-decimal.
        ``client_reference_id`` echoes our order number back on the webhook.
        """
        try:
            session = stripe.checkout.Session.create(
                api_key=self.secret_key,
                mode="payment",
                line_items=[
                    {
                        "price_data": {
                            "currency": currency.lower(),
                            "product_data": {"name": description},
                            "unit_amount": amount_minor,
                        },
                        "quantity": 1,
                    },
                ],
                client_reference_id=ext_order_id,
                customer_email=buyer_email,
                metadata={"order_number": ext_order_id},
                success_url=success_url,
                cancel_url=cancel_url,
            )
        except stripe.StripeError as exc:
            raise StripeError(f"Stripe create-session failed: {exc}") from exc

        if not session.url or not session.id:
            raise StripeError("Stripe create-session response missing url/id")
        return {"checkout_url": session.url, "session_id": session.id}

    def construct_event(self, raw_body: bytes, signature_header: str | None) -> stripe.Event:
        """Verify the ``Stripe-Signature`` header and return the parsed event.

        Delegates to the SDK's ``construct_event`` (HMAC-SHA256 of
        ``timestamp.payload`` against the webhook secret, with timestamp-tolerance
        checking). Raises ``StripeError`` on a missing/forged signature or
        malformed payload — the security boundary; an unverified body is never
        trusted.
        """
        if not signature_header:
            raise StripeError("Missing Stripe-Signature header")
        try:
            return stripe.Webhook.construct_event(
                payload=raw_body,
                sig_header=signature_header,
                secret=self.webhook_secret,
            )
        except (ValueError, stripe.SignatureVerificationError) as exc:
            raise StripeError("Invalid Stripe webhook signature") from exc


def get_stripe_service() -> StripeService:
    """FastAPI-style accessor; raises 503 when Stripe is unconfigured."""
    missing = [name for name in _STRIPE_REQUIRED_FIELDS if not getattr(settings, name)]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Stripe not configured — missing settings: {', '.join(missing)}. "
                "Set these in .env to enable payments."
            ),
        )
    return StripeService(
        secret_key=settings.STRIPE_SECRET_KEY,
        webhook_secret=settings.STRIPE_WEBHOOK_SECRET,
    )
