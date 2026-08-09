"""Collector for Content-Security-Policy violation reports.

The browser — not the frontend client — posts here, which drives every
design choice in this module:

- **Not in the OpenAPI schema.** No API consumer ever calls this, so
  publishing it would only add a phantom operation to the generated
  frontend client (`api.gen.ts`).
- **Unauthenticated.** The reporting agent sends no credentials and
  ignores redirects; requiring auth would silently discard every report.
- **Always 204, whatever arrives.** Browsers discard the response body, so
  there is nothing to say. Validating loudly would only teach a prober
  which payloads we parse.
- **Two body shapes.** The legacy `report-uri` directive posts
  `application/csp-report` with a single `{"csp-report": {...}}` object in
  kebab-case; the modern Reporting API posts `application/reports+json`
  with a JSON *array* of envelopes whose `body` is camelCase. Both are in
  the wild simultaneously — see `build_csp`.

Being public and unauthenticated, it is also a log-flooding and
log-injection vector, so reports are rate-limited, size-capped, and every
field is scrubbed of control characters before it reaches the logger.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Response, status

from app.core.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter()

#: Largest report body we will parse. Real violation reports are well under
#: 2 KB; anything larger is a mistake or an attempt to flood the log.
MAX_REPORT_BYTES = 16_384

#: Per-field cap in the emitted log line. `blocked-uri` can legitimately be a
#: multi-kilobyte `data:` URL, which is not worth logging in full.
MAX_FIELD_CHARS = 200

#: Stop one report from becoming an unbounded run of log lines.
MAX_REPORTS_PER_REQUEST = 10

#: Report fields worth logging, as (log label, legacy key, Reporting-API key).
#: The two dialects disagree on both case and, for the blocked resource, on
#: the word itself (`blocked-uri` vs `blockedURL`).
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("document", "document-uri", "documentURL"),
    ("directive", "effective-directive", "effectiveDirective"),
    ("blocked", "blocked-uri", "blockedURL"),
    ("source", "source-file", "sourceFile"),
    ("line", "line-number", "lineNumber"),
    ("disposition", "disposition", "disposition"),
)


def _scrub(value: Any) -> str:
    """Render a report field as a single safe, bounded log token.

    Newlines and carriage returns in attacker-controlled report fields would
    otherwise forge whole log lines, so control characters are dropped rather
    than escaped.
    """
    text = str(value)
    cleaned = "".join(ch for ch in text if ch.isprintable())
    if len(cleaned) > MAX_FIELD_CHARS:
        return cleaned[:MAX_FIELD_CHARS] + "…"
    return cleaned


def _extract_reports(payload: Any) -> list[dict[str, Any]]:
    """Normalise either wire format into a list of report bodies."""
    if isinstance(payload, dict):
        legacy = payload.get("csp-report")
        if isinstance(legacy, dict):
            return [legacy]
        # A bare object that is not a legacy envelope: treat it as the body
        # itself so a slightly-off client still gets logged.
        return [payload]
    if isinstance(payload, list):
        bodies: list[dict[str, Any]] = []
        for envelope in payload:
            if not isinstance(envelope, dict):
                continue
            # Reporting API envelopes carry other report types too (deprecation,
            # intervention, network-error logging) on the same endpoint.
            if envelope.get("type") not in (None, "csp-violation"):
                continue
            body = envelope.get("body")
            bodies.append(body if isinstance(body, dict) else envelope)
        return bodies
    return []


@router.post(
    "/csp-report",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
)
@limiter.limit("120/hour")
async def collect_csp_report(request: Request) -> Response:
    """Log a browser-reported CSP violation. Always answers 204."""
    body = await request.body()

    if not body or len(body) > MAX_REPORT_BYTES:
        # Oversized bodies are dropped without parsing — logging their size is
        # enough to notice a flood without paying to deserialize it.
        if body:
            logger.debug("CSP report dropped: oversized (%d bytes)", len(body))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        logger.debug("CSP report dropped: body was not JSON")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    for report in _extract_reports(payload)[:MAX_REPORTS_PER_REQUEST]:
        fields = " ".join(
            f"{label}={_scrub(value)}"
            for label, legacy_key, modern_key in _FIELDS
            if (value := report.get(legacy_key, report.get(modern_key))) not in (None, "")
        )
        logger.warning("CSP violation: %s", fields or "(no recognised fields)")

    return Response(status_code=status.HTTP_204_NO_CONTENT)
