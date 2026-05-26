from app.schemas.base import BaseSchema


class PayUCheckoutResponse(BaseSchema):
    """Where to send the buyer to pay — PayU's hosted payment page."""

    redirect_uri: str


class StripeCheckoutResponse(BaseSchema):
    """Where to send the buyer to pay — Stripe's hosted Checkout page."""

    checkout_url: str


class StripePaymentIntentResponse(BaseSchema):
    """Client secret the browser confirms — in-page Express Checkout (wallets)."""

    client_secret: str


class StripeConfigResponse(BaseSchema):
    """Browser-side Stripe config for the Express Checkout Element.

    ``publishable_key`` is safe to expose (it's a browser key by design).
    ``configured`` lets the frontend hide the wallet widget when Stripe has no
    keys, rather than rendering a dead button.
    """

    configured: bool
    publishable_key: str
