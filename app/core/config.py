import logging
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Both `SECRET_KEY` and `JWT_SECRET_KEY` are HMAC keys; OWASP guidance is
# >= 256 bits (32 bytes) of entropy. Enforced at startup so a misconfigured
# env never silently weakens token signing.
_MIN_SECRET_BYTES = 32


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    # Application
    APP_NAME: str = "E-commerce API"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = True
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    # SQLAlchemy engine ``echo`` (logs every statement WITH bound parameter
    # values → PII in prod logs). Decoupled from DEBUG so verbose SQL is never
    # tied to the broad flag. ``None`` inherits DEBUG for dev convenience; set
    # explicitly (true/false) to override.
    SQL_ECHO: bool | None = None
    SECRET_KEY: str

    # Database
    DATABASE_URL: str

    # JWT
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14
    # Admin "view as customer" tokens: short-lived, no refresh (bounded blast radius).
    IMPERSONATION_TOKEN_EXPIRE_MINUTES: int = 30
    # Kill-switch: set false to disable impersonation in an environment without a
    # deploy (the endpoint 503s). On by default.
    IMPERSONATION_ENABLED: bool = True
    JWT_ISSUER: str = "ecommerce-store"
    JWT_AUDIENCE: str = "ecommerce-store-clients"

    # Shared secret gating the server-to-server SSR session-validation endpoint
    # (`POST /auth/refresh/ssr`). Known only to the trusted Nuxt SSR server, which
    # sends it as the `X-SSR-Secret` header. That endpoint validates the refresh
    # cookie and mints an access token WITHOUT rotating — so it must never be
    # reachable by an ordinary client (a stolen cookie could otherwise mint access
    # tokens indefinitely, bypassing rotation/reuse-detection). Empty = disabled
    # (the endpoint 403s), which fails closed: SSR simply falls back to rendering
    # the logged-out shell and the client restores the session after hydration.
    SSR_SHARED_SECRET: str = ""

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # Rate limiting (slowapi). Use ``memory://`` for single-instance dev,
    # ``redis://host:port/db`` for multi-worker / production deployments.
    RATE_LIMIT_STORAGE_URL: str = "memory://"
    # E2E escape hatch: disables ALL slowapi rate limiting so back-to-back
    # E2E runs don't trip the /auth/login 5/15min budget. Double-gated — it
    # only takes effect when ENVIRONMENT=development, so production can never
    # disable rate limiting by accident. Off by default.
    E2E_DISABLE_RATE_LIMIT: bool = False

    # B2B net-terms dunning (overdue pay-on-account reminders). Transactional
    # (about an existing invoice debt, not marketing → no consent gate), but
    # ships DARK behind an env kill-switch so a store opts in explicitly. The
    # detection pass (``app.scripts.send_net_terms_reminders``, OS-cron driven)
    # emails the buyer of each overdue, unpaid, non-cancelled net-terms order,
    # re-sending at most once per ``NET_TERMS_DUNNING_INTERVAL_DAYS`` (deduped
    # via a ``net_terms_reminder`` order_event — no schema change).
    NET_TERMS_DUNNING_ENABLED: bool = False
    NET_TERMS_DUNNING_INTERVAL_DAYS: int = 7

    # Cloudflare R2
    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET_NAME: str = ""
    R2_PUBLIC_URL: str = ""

    # Email / Resend
    RESEND_API_KEY: str = ""
    EMAIL_FROM: str = "noreply@ecommerce.com"
    EMAIL_FROM_NAME: str = "E-commerce Store"
    FRONTEND_URL: str = "http://localhost:3000"
    # Public base URL of this backend — used to build the PayU notifyUrl
    # (PayU's servers must reach it; set to the public URL in prod).
    BACKEND_URL: str = "http://localhost:8000"

    # PayU
    PAYU_POS_ID: str = ""
    PAYU_CLIENT_ID: str = ""
    PAYU_CLIENT_SECRET: str = ""
    # "Second key (MD5)" from the PayU POS — used to verify notification signatures.
    PAYU_SECOND_KEY: str = ""
    PAYU_SANDBOX: bool = True

    # Stripe
    STRIPE_SECRET_KEY: str = ""
    STRIPE_PUBLISHABLE_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""

    # Meilisearch (storefront search). Both default to "" so dev/CI/tests boot
    # without a running engine — search endpoints 503 and index sync no-ops until
    # configured. Set MEILISEARCH_URL to e.g. http://10.0.0.1:7700 in .env.
    MEILISEARCH_URL: str = ""
    MEILISEARCH_MASTER_KEY: str = ""

    # Invoice / store identity — printed on generated order invoices. All have
    # defaults so dev/CI/tests boot without configuration; set real values in
    # .env for production-looking invoices.
    STORE_NAME: str = "E-commerce Store"
    STORE_ADDRESS_LINE: str = ""  # street + building number
    STORE_POSTAL_CITY: str = ""  # e.g. "00-001 Warszawa"
    STORE_COUNTRY: str = ""
    STORE_TAX_ID: str = ""  # NIP / VAT identifier
    # The country whose VAT the store charges. Distinct from the free-text
    # ``STORE_COUNTRY`` above (a display string on the faktura) — this is an
    # ISO-2 code used to resolve rates, and today every sale is taxed here
    # regardless of destination (single-jurisdiction model).
    TAX_HOME_COUNTRY: str = "PL"
    STORE_EMAIL: str = ""
    # Gross-inclusive VAT rate (%) used to decompose net/VAT on invoices.
    # 0 → no VAT block (plain receipt). Applied uniformly across currencies in
    # v1 — per-currency VAT is a future refinement.
    INVOICE_VAT_RATE: int = 23
    # Sequential faktura numbering. The number is rendered from a Python str.format
    # template with the placeholders {prefix} {year} {month} {seq} — e.g. {seq:05d}
    # zero-pads. Custom/hand-written templates are supported (validated at startup).
    # The sequence resets each calendar year (the counter is keyed by year), so
    # {month} is display-only — a monthly reset would be a future enhancement.
    INVOICE_NUMBER_PREFIX: str = "FV"
    INVOICE_NUMBER_FORMAT: str = "{prefix}/{year}/{seq:05d}"
    # Faktura korygująca (PL corrective invoice) numbering. Distinct counter +
    # series from the faktura — a refund must never consume a faktura number.
    # Same template grammar as INVOICE_NUMBER_FORMAT; validated at startup.
    INVOICE_KOR_NUMBER_PREFIX: str = "KOR"
    INVOICE_KOR_NUMBER_FORMAT: str = "{prefix}/{year}/{seq:05d}"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="allow")

    @property
    def sql_echo(self) -> bool:
        """Effective SQLAlchemy ``echo``: explicit ``SQL_ECHO`` else ``DEBUG``."""
        return self.DEBUG if self.SQL_ECHO is None else self.SQL_ECHO

    @property
    def rate_limit_enabled(self) -> bool:
        """Effective slowapi state: disabled only when BOTH gates hold —
        ``ENVIRONMENT=development`` AND the explicit ``E2E_DISABLE_RATE_LIMIT``
        opt-in. Any other combination keeps rate limiting on."""
        return not (self.ENVIRONMENT == "development" and self.E2E_DISABLE_RATE_LIMIT)

    @model_validator(mode="after")
    def _no_debug_in_production(self) -> "Settings":
        # A prod deploy that forgets DEBUG=false would log SQL (incl. bound PII)
        # via the engine echo and expose verbose internals. Fail loud at startup
        # rather than leaking silently for the life of the deploy.
        if self.ENVIRONMENT == "production" and self.DEBUG:
            raise ValueError(
                "DEBUG must be false when ENVIRONMENT=production "
                "(set DEBUG=false in the production environment)."
            )
        # SEC-005: memory:// rate-limit buckets are per-process, so with >1
        # uvicorn worker each keeps its own counters and the effective limit is
        # N x configured (login/reset/refresh throttles materially weaker than
        # they read). Warn rather than raise — a single-worker prod is valid.
        if self.ENVIRONMENT == "production" and self.RATE_LIMIT_STORAGE_URL.startswith("memory://"):
            logger.warning(
                "RATE_LIMIT_STORAGE_URL is memory:// in production — rate limits "
                "are per-process and will not hold across multiple uvicorn workers. "
                "Set a redis:// URL for a multi-worker deploy."
            )
        # The E2E rate-limit kill-switch is double-gated on development; setting
        # it anywhere else is a no-op. Warn so a misconfigured staging/CI env
        # ("why is my flag not working?") is diagnosable from the startup log.
        if self.E2E_DISABLE_RATE_LIMIT and self.ENVIRONMENT != "development":
            logger.warning(
                "E2E_DISABLE_RATE_LIMIT is set but ENVIRONMENT=%s — the flag only "
                "takes effect in development; rate limiting stays ENABLED.",
                self.ENVIRONMENT,
            )
        return self

    @field_validator("SECRET_KEY", "JWT_SECRET_KEY")
    @classmethod
    def _enforce_secret_length(cls, value: str, info) -> str:
        if len(value.encode("utf-8")) < _MIN_SECRET_BYTES:
            raise ValueError(
                f"{info.field_name} must be at least {_MIN_SECRET_BYTES} bytes "
                f"(got {len(value.encode('utf-8'))}). Generate with "
                f"`python -c 'import secrets; print(secrets.token_urlsafe(48))'`."
            )
        return value

    @field_validator("INVOICE_NUMBER_FORMAT", "INVOICE_KOR_NUMBER_FORMAT")
    @classmethod
    def _validate_invoice_number_format(cls, value: str, info) -> str:
        # Fail loud at startup on a malformed template (unknown placeholder or bad
        # format spec) rather than producing garbage invoice numbers at issue time.
        try:
            value.format(prefix="X", year=2026, month=1, seq=1)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                f"{info.field_name} is invalid. Use a str.format template with "
                "the placeholders {prefix} {year} {month} {seq} (e.g. "
                f'"{{prefix}}/{{year}}/{{seq:05d}}"). Error: {exc}'
            ) from exc
        return value

    @field_validator("R2_SECRET_ACCESS_KEY")
    @classmethod
    def _enforce_r2_secret_length(cls, value: str, info) -> str:
        # R2 secret keys are 64-char hex strings; defaults to "" so dev environments
        # without R2 still boot. Only enforce minimum length when non-empty so a
        # misconfigured (truncated) secret fails loudly at startup rather than at
        # the first upload attempt.
        if value and len(value.encode("utf-8")) < _MIN_SECRET_BYTES:
            raise ValueError(
                f"{info.field_name} must be at least {_MIN_SECRET_BYTES} bytes "
                f"(got {len(value.encode('utf-8'))}). Copy the full secret from "
                "Cloudflare R2 dashboard."
            )
        return value


settings = Settings()  # pyright: ignore[reportCallIssue] -- pydantic-settings loads required fields from env
