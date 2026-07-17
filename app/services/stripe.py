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


def _as_id(value: object) -> str | None:
    """Normalize a Stripe field that may be an id string or an expanded object → id."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return getattr(value, "id", None)


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

    def create_payment_intent(
        self,
        *,
        ext_order_id: str,
        amount_minor: int,
        currency: str,
        description: str,
        buyer_email: str,
    ) -> dict:
        """Create a PaymentIntent for the in-page Express Checkout Element.

        Returns ``{client_secret, payment_intent_id}``. The client secret is
        confirmed browser-side by Stripe.js (Apple Pay / Google Pay / Link); the
        order is settled server-side from the ``payment_intent.succeeded`` webhook,
        keyed on ``metadata.order_number``. ``automatic_payment_methods`` is what
        surfaces the wallets (driven by the dashboard's enabled methods). One
        aggregate ``amount_minor`` so the charge can't drift from ``order.total``.
        """
        try:
            intent = stripe.PaymentIntent.create(
                api_key=self.secret_key,
                amount=amount_minor,
                currency=currency.lower(),
                automatic_payment_methods={"enabled": True},
                description=description,
                receipt_email=buyer_email,
                metadata={"order_number": ext_order_id},
            )
        except stripe.StripeError as exc:
            raise StripeError(f"Stripe create-payment-intent failed: {exc}") from exc

        if not intent.client_secret or not intent.id:
            raise StripeError("Stripe create-payment-intent response missing client_secret/id")
        return {"client_secret": intent.client_secret, "payment_intent_id": intent.id}

    # --- B2B net-terms auto-charge: card vaulting + off-session charge (Program C) ---

    def create_customer(self, *, email: str | None, name: str | None, ext_ref: str) -> str:
        """Create a Stripe Customer for a company's vaulted card; returns its id.

        ``ext_ref`` (our ``company:{id}``) is stored in metadata for traceability.
        """
        params: dict = {"api_key": self.secret_key, "metadata": {"ext_ref": ext_ref}}
        if email:
            params["email"] = email
        if name:
            params["name"] = name
        try:
            customer = stripe.Customer.create(**params)
        except stripe.StripeError as exc:
            raise StripeError(f"Stripe create-customer failed: {exc}") from exc
        if not customer.id:
            raise StripeError("Stripe create-customer response missing id")
        return customer.id

    def create_setup_intent(self, *, customer_id: str) -> dict:
        """Create an off-session-capable SetupIntent to vault a card for auto-pay.

        Returns ``{client_secret, id}``. The browser confirms it with the Payment
        Element (setup mode); the resulting PaymentMethod is then read back via
        ``retrieve_setup_intent`` and stored on the company. ``usage="off_session"``
        marks the mandate so later cron charges are permitted without the buyer.
        """
        try:
            intent = stripe.SetupIntent.create(
                api_key=self.secret_key,
                customer=customer_id,
                usage="off_session",
                payment_method_types=["card"],
            )
        except stripe.StripeError as exc:
            raise StripeError(f"Stripe create-setup-intent failed: {exc}") from exc
        if not intent.client_secret or not intent.id:
            raise StripeError("Stripe create-setup-intent response missing client_secret/id")
        return {"client_secret": intent.client_secret, "id": intent.id}

    def retrieve_setup_intent(self, *, setup_intent_id: str) -> dict:
        """Read a confirmed SetupIntent; returns ``{status, payment_method, customer}``.

        The caller stores ``payment_method`` on the company only when ``status`` is
        ``succeeded``. ``payment_method`` / ``customer`` may be ids or expanded
        objects depending on the SDK — normalized to ids here.
        """
        try:
            intent = stripe.SetupIntent.retrieve(setup_intent_id, api_key=self.secret_key)
        except stripe.StripeError as exc:
            raise StripeError(f"Stripe retrieve-setup-intent failed: {exc}") from exc
        return {
            "status": intent.status,
            "payment_method": _as_id(intent.payment_method),
            "customer": _as_id(intent.customer),
        }

    def charge_off_session(
        self,
        *,
        customer_id: str,
        payment_method_id: str,
        amount_minor: int,
        currency: str,
        ext_order_id: str,
        idempotency_key: str,
    ) -> dict:
        """Off-session charge of a vaulted card for a due net-terms order.

        Confirms immediately (``confirm=True, off_session=True``) so the result is
        synchronous. Returns ``{status, id}`` — ``status == "succeeded"`` means the
        caller stamps the order paid. ``idempotency_key`` (our order id) makes a
        retried run a no-op instead of a double charge. A declined card raises
        ``StripeError`` (the caller leaves the order unpaid for dunning/manual).
        """
        try:
            intent = stripe.PaymentIntent.create(
                api_key=self.secret_key,
                amount=amount_minor,
                currency=currency.lower(),
                customer=customer_id,
                payment_method=payment_method_id,
                confirm=True,
                off_session=True,
                description=f"Net-terms auto-charge {ext_order_id}",
                metadata={"order_number": ext_order_id, "kind": "net_terms_auto_charge"},
                idempotency_key=idempotency_key,
            )
        except stripe.StripeError as exc:
            raise StripeError(f"Stripe off-session charge failed: {exc}") from exc
        if not intent.id:
            raise StripeError("Stripe off-session charge response missing id")
        return {"status": intent.status, "id": intent.id}

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
        except stripe.SignatureVerificationError as exc:
            raise StripeError("Invalid Stripe webhook signature") from exc
        except Exception as exc:
            # The signature is already verified above, so any failure here is a
            # malformed / unexpectedly-shaped (but correctly-signed) body — e.g. the
            # SDK reads ``event.object`` and the payload omits it (AttributeError), or
            # bad JSON (ValueError). Degrade to StripeError so the route returns 400,
            # never a 500 (a 500 would also make Stripe retry). Safe to catch broadly:
            # construct_event only parses + verifies — no I/O, no DB.
            raise StripeError("Malformed Stripe webhook payload") from exc


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
