"""PayU (Polish market) payment integration — REST API, hosted-redirect flow.

Flow: the backend authorizes (OAuth client-credentials), creates a PayU order
for one of our pending orders, and returns PayU's hosted ``redirectUri`` for the
buyer. After payment PayU POSTs a signed notification to our webhook; we verify
the ``OpenPayU-Signature`` against the POS "second key" before trusting it and
flip the order ``pending → paid``.

``get_payu_client`` raises ``HTTPException(503)`` when PayU is unconfigured so
dev/CI boot and every other endpoint works without PayU keys (mirrors
``get_r2_client``). Live behaviour is exercised against the PayU sandbox once
keys are set; the unit/integration tests mock the PayU HTTP API via ``respx``.
"""

import hashlib
import hmac

import httpx
from fastapi import HTTPException, status

from app.core.config import settings

_PAYU_REQUIRED_FIELDS = (
    "PAYU_POS_ID",
    "PAYU_CLIENT_ID",
    "PAYU_CLIENT_SECRET",
    "PAYU_SECOND_KEY",
)

_SANDBOX_BASE = "https://secure.snd.payu.com"
_PRODUCTION_BASE = "https://secure.payu.com"

# PayU's notification signature algorithms (the POS "second key" defaults to MD5).
_SIGNATURE_ALGORITHMS = {"MD5": "md5", "SHA-1": "sha1", "SHA-256": "sha256"}


class PayUError(Exception):
    """A PayU API call failed or returned an unexpected shape."""


def payu_configured() -> bool:
    return all(getattr(settings, name) for name in _PAYU_REQUIRED_FIELDS)


class PayUClient:
    """Thin async client over the PayU REST API.

    One instance per request (built by ``get_payu_client``). Tokens are fetched
    per ``create_order`` call — fine at our volume and avoids stale-token logic.
    """

    def __init__(
        self,
        *,
        pos_id: str,
        client_id: str,
        client_secret: str,
        second_key: str,
        sandbox: bool,
    ) -> None:
        self.pos_id = pos_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.second_key = second_key
        self.base_url = _SANDBOX_BASE if sandbox else _PRODUCTION_BASE

    async def _authorize(self, http: httpx.AsyncClient) -> str:
        resp = await http.post(
            f"{self.base_url}/pl/standard/user/oauth/authorize",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise PayUError(f"PayU OAuth failed ({resp.status_code})")
        token = resp.json().get("access_token")
        if not token:
            raise PayUError("PayU OAuth response missing access_token")
        return token

    async def create_order(
        self,
        *,
        ext_order_id: str,
        amount_minor: int,
        currency: str,
        description: str,
        buyer_email: str,
        customer_ip: str,
        products: list[dict],
        notify_url: str,
        continue_url: str,
    ) -> dict:
        """Create a PayU order; returns ``{redirect_uri, payu_order_id}``.

        ``amount_minor`` and each product ``unitPrice`` are integer minor units
        (grosze) as PayU requires. PayU answers the create-order POST with a 302
        whose JSON body carries the ``redirectUri`` — we must not follow it.
        """
        payload = {
            "customerIp": customer_ip,
            "merchantPosId": self.pos_id,
            "description": description,
            "currencyCode": currency,
            "totalAmount": str(amount_minor),
            "extOrderId": ext_order_id,
            "notifyUrl": notify_url,
            "continueUrl": continue_url,
            "buyer": {"email": buyer_email},
            "products": products,
        }
        # follow_redirects=False: keep PayU's 302 (its body holds redirectUri).
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as http:
            token = await self._authorize(http)
            resp = await http.post(
                f"{self.base_url}/api/v2_1/orders",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )

        if resp.status_code not in (200, 201, 302):
            raise PayUError(f"PayU create-order failed ({resp.status_code})")
        body = resp.json()
        if body.get("status", {}).get("statusCode") != "SUCCESS":
            raise PayUError(f"PayU create-order rejected: {body.get('status')}")
        redirect_uri = body.get("redirectUri")
        payu_order_id = body.get("orderId")
        if not redirect_uri or not payu_order_id:
            raise PayUError("PayU create-order response missing redirectUri/orderId")
        return {"redirect_uri": redirect_uri, "payu_order_id": payu_order_id}

    def verify_notification(self, raw_body: bytes, signature_header: str | None) -> bool:
        """Verify a PayU notification's ``OpenPayU-Signature`` header.

        Header shape: ``sender=...;signature=<hex>;algorithm=MD5;content=DOCUMENT``.
        The expected hash is ``algo(raw_body + second_key)``. Constant-time compare.
        """
        if not signature_header:
            return False
        parts: dict[str, str] = {}
        for segment in signature_header.split(";"):
            key, _, value = segment.partition("=")
            if value:
                parts[key.strip()] = value.strip()
        provided = parts.get("signature")
        algo_name = _SIGNATURE_ALGORITHMS.get(parts.get("algorithm", "MD5").upper())
        if not provided or not algo_name:
            return False
        digest = hashlib.new(algo_name, usedforsecurity=False)
        digest.update(raw_body + self.second_key.encode("utf-8"))
        return hmac.compare_digest(digest.hexdigest().lower(), provided.lower())


def get_payu_client() -> PayUClient:
    """FastAPI-style accessor; raises 503 when PayU is unconfigured."""
    missing = [name for name in _PAYU_REQUIRED_FIELDS if not getattr(settings, name)]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"PayU not configured — missing settings: {', '.join(missing)}. "
                "Set these in .env to enable payments."
            ),
        )
    return PayUClient(
        pos_id=settings.PAYU_POS_ID,
        client_id=settings.PAYU_CLIENT_ID,
        client_secret=settings.PAYU_CLIENT_SECRET,
        second_key=settings.PAYU_SECOND_KEY,
        sandbox=settings.PAYU_SANDBOX,
    )
