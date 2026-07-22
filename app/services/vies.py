"""EU VIES VAT-ID validation (Phase-0 tax U3).

VIES is the EU's VAT Information Exchange System — the authority on whether a VAT
number is currently registered. This module wraps its public REST API and caches
the outcome on the ``companies`` row.

**Never called on the checkout path.** Decisions 5 & 6 of the tax plan: the
VAT-ID is captured on the company record and validated by an *explicit* admin or
portal action plus a periodic re-check cron. Checkout reads the stored
``vies_valid`` + ``vies_confirmed_at`` — so a VIES outage can delay a company's
onboarding but can never delay or mis-tax a sale.

**Fail-closed.** A transient outage raises :class:`VIESUnavailableError`; the
caller records the *attempt* (``vies_last_checked_at``) but leaves an existing
confirmation untouched — an outage must not silently drop a good confirmation,
nor invent one. An unconfirmed company is domestic-taxed (the safe default).

The client is deliberately **synchronous** (``httpx.Client``): the same code path
serves the async admin/portal action (an infrequent button press) and the sync
re-check cron, and there is no checkout-latency budget to protect here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.vat_id import is_vat_id, normalize_vat_id, split_vat_id
from app.models.company import Company

logger = logging.getLogger(__name__)

# VIES ``userError`` values that mean "couldn't answer", not "answered no". These
# are transient/infrastructure conditions — fail closed and let the caller retry.
_UNAVAILABLE_ERRORS = frozenset(
    {
        "MS_UNAVAILABLE",
        "SERVICE_UNAVAILABLE",
        "TIMEOUT",
        "SERVER_BUSY",
        "MS_MAX_CONCURRENT_REQ",
        "GLOBAL_MAX_CONCURRENT_REQ",
    }
)


class VIESError(Exception):
    """Base for VIES failures."""


class VIESValidationError(VIESError):
    """The request itself was wrong (no VAT-ID, malformed, or VIES ``INVALID_INPUT``)."""


class VIESUnavailableError(VIESError):
    """VIES (or the member state) could not answer right now — retry later.

    Distinct from "answered: not valid". Fail-closed: the caller records the
    attempt but must not change an existing confirmation on this.
    """


@dataclass(frozen=True)
class VIESResult:
    """A resolved VIES answer (the service *did* respond)."""

    valid: bool
    name: str | None
    address: str | None
    request_id: str | None


def _utcnow() -> datetime:
    """Naive UTC, matching every other datetime column in the schema."""
    return datetime.now(UTC).replace(tzinfo=None)


def _requester_fields() -> dict[str, str]:
    """Our own EU VAT-ID as the VIES *requester*, so VIES returns a consultation
    ``requestIdentifier`` we keep as proof-of-check. Optional — omitted when
    ``VIES_REQUESTER_VAT_ID`` is unset (validation still works, just no proof)."""
    raw = settings.VIES_REQUESTER_VAT_ID.strip()
    if not raw:
        return {}
    normalized = normalize_vat_id(raw)
    if not is_vat_id(normalized):
        logger.warning("VIES_REQUESTER_VAT_ID %r is malformed; sending no requester", raw)
        return {}
    country, number = split_vat_id(normalized)
    return {"requesterMemberStateCode": country, "requesterNumber": number}


def check_vat(vat_id: str) -> VIESResult:
    """Ask VIES whether ``vat_id`` is a registered VAT number.

    Raises :class:`VIESValidationError` for a malformed id or a VIES
    ``INVALID_INPUT``, and :class:`VIESUnavailableError` for any network error,
    non-2xx response, or transient VIES ``userError``.
    """
    normalized = normalize_vat_id(vat_id)
    if not is_vat_id(normalized):
        raise VIESValidationError(f"'{vat_id}' is not a well-formed EU VAT-ID")
    country, number = split_vat_id(normalized)

    payload = {"countryCode": country, "vatNumber": number, **_requester_fields()}
    url = f"{settings.VIES_REST_URL.rstrip('/')}/check-vat-number"
    try:
        with httpx.Client(timeout=settings.VIES_TIMEOUT_SECONDS) as http:
            response = http.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:  # network, non-2xx, bad JSON
        raise VIESUnavailableError(f"VIES request failed: {exc}") from exc

    user_error = (data.get("userError") or "").upper()
    if user_error in _UNAVAILABLE_ERRORS:
        raise VIESUnavailableError(f"VIES returned {user_error}")
    if user_error == "INVALID_INPUT":
        raise VIESValidationError("VIES rejected the VAT-ID as malformed (INVALID_INPUT)")

    return VIESResult(
        valid=bool(data.get("valid")),
        name=(data.get("name") or None),
        address=(data.get("address") or None),
        request_id=(data.get("requestIdentifier") or None),
    )


def validate_company_vat_id(db: Session, company: Company) -> Company:
    """Run a VIES check for ``company`` and cache the outcome on the row.

    Records the attempt (``vies_last_checked_at``) even when VIES is unavailable,
    then re-raises so the caller can report "try again". On a definitive answer,
    ``vies_valid`` is set; a positive answer additionally stamps
    ``vies_confirmed_at`` / name / proof, a negative answer clears them.

    Does **not** check ``settings.VIES_ENABLED`` — that kill-switch is the
    caller's gate (the admin/portal endpoints 503, the cron no-ops), keeping this
    unit testable on its own. Flushes but does not commit (the route owns the txn).
    """
    if not company.vat_id:
        raise VIESValidationError("company has no vat_id to validate")

    now = _utcnow()
    company.vies_last_checked_at = now
    try:
        result = check_vat(company.vat_id)
    except VIESUnavailableError:
        db.flush()  # persist the attempt timestamp; leave any confirmation intact
        raise

    company.vies_valid = result.valid
    if result.valid:
        company.vies_confirmed_at = now
        company.vies_confirmed_name = result.name
        company.vies_request_id = result.request_id
    else:
        company.vies_confirmed_at = None
        company.vies_confirmed_name = None
        company.vies_request_id = None
    db.flush()
    return company


def recheck_stale_confirmations(db: Session, *, older_than_days: int | None = None) -> int:
    """Re-validate confirmed companies whose confirmation is getting old.

    The periodic half of U3 (OS-cron driven). It refreshes a confirmation before
    the freshness window lapses AND catches a company that has *de-registered*
    since (VIES now answers "not valid" → ``vies_valid`` flips false). A healthy
    no-op when ``VIES_ENABLED`` is off. Commits per company so one bad row doesn't
    lose the batch; a VIES outage on a row records the attempt and moves on.

    ``older_than_days`` defaults to ``VIES_CONFIRMATION_TTL_DAYS``; pass a smaller
    value to refresh earlier (before the U4 freshness check would treat it stale).
    Returns the count of companies re-checked (attempted).
    """
    if not settings.VIES_ENABLED:
        return 0
    threshold = (
        older_than_days if older_than_days is not None else settings.VIES_CONFIRMATION_TTL_DAYS
    )
    cutoff = _utcnow() - timedelta(days=threshold)
    companies = list(
        db.scalars(
            select(Company).where(
                Company.vat_id.is_not(None),
                Company.vies_valid.is_(True),
                (Company.vies_confirmed_at.is_(None)) | (Company.vies_confirmed_at < cutoff),
            )
        ).all()
    )
    rechecked = 0
    for company in companies:
        try:
            validate_company_vat_id(db, company)
            db.commit()
            rechecked += 1
        except VIESUnavailableError:
            db.commit()  # attempt recorded (vies_last_checked_at); retry next run
            logger.warning("VIES unavailable re-checking company %s; will retry", company.id)
        except VIESError as exc:
            db.rollback()
            logger.warning("VIES re-check failed for company %s: %s", company.id, exc)
    return rechecked
