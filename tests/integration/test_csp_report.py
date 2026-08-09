"""Tests for the CSP violation collector (`app/api/v1/csp_report.py`).

The endpoint is written to be unfailable — a browser must never receive
anything but 204 — so almost every assertion here is about what reaches the
*log*, not about the status code. `caplog` is the real subject.
"""

import json
import logging

import pytest
from starlette.testclient import TestClient

from app.api.v1.csp_report import MAX_FIELD_CHARS, MAX_REPORT_BYTES
from app.core.config import settings
from app.main import app as live_app

pytestmark = pytest.mark.usefixtures("reset_limiter")

ENDPOINT = "/api/v1/csp-report"

LEGACY_REPORT = {
    "csp-report": {
        "document-uri": "https://store.example.com/products/widget",
        "effective-directive": "script-src",
        "blocked-uri": "https://evil.example.com/x.js",
        "source-file": "https://store.example.com/app.js",
        "line-number": 42,
        "disposition": "report",
    }
}

MODERN_REPORT = [
    {
        "type": "csp-violation",
        "age": 0,
        "url": "https://store.example.com/cart",
        "body": {
            "documentURL": "https://store.example.com/cart",
            "effectiveDirective": "img-src",
            "blockedURL": "https://tracker.example.com/pixel.gif",
            "disposition": "report",
        },
    }
]


def _post(body, content_type: str = "application/csp-report"):
    with TestClient(live_app) as tc:
        return tc.post(ENDPOINT, content=body, headers={"content-type": content_type})


def test_legacy_report_uri_payload_is_logged(caplog):
    """`report-uri` posts a kebab-case `{"csp-report": {...}}` envelope."""
    with caplog.at_level(logging.WARNING, logger="app.api.v1.csp_report"):
        response = _post(json.dumps(LEGACY_REPORT))

    assert response.status_code == 204
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "directive=script-src" in message
    assert "blocked=https://evil.example.com/x.js" in message
    assert "document=https://store.example.com/products/widget" in message
    assert "line=42" in message


def test_reporting_api_payload_is_logged(caplog):
    """`report-to` posts a camelCase array of envelopes — a different dialect
    of the same information, and the one Chrome sends."""
    with caplog.at_level(logging.WARNING, logger="app.api.v1.csp_report"):
        response = _post(json.dumps(MODERN_REPORT), content_type="application/reports+json")

    assert response.status_code == 204
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "directive=img-src" in message
    assert "blocked=https://tracker.example.com/pixel.gif" in message


def test_non_csp_reporting_api_types_are_ignored(caplog):
    """The Reporting API multiplexes deprecation/intervention reports onto the
    same endpoint. Logging those as CSP violations would poison the watch
    window that gates the enforce flip."""
    payload = [{"type": "deprecation", "body": {"id": "TheThing", "message": "gone soon"}}]
    with caplog.at_level(logging.WARNING, logger="app.api.v1.csp_report"):
        response = _post(json.dumps(payload), content_type="application/reports+json")

    assert response.status_code == 204
    assert caplog.records == []


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json at all",
        b"{",
        b"\xff\xfe\x00",
        json.dumps("a bare string").encode(),
        json.dumps(12345).encode(),
    ],
    ids=["empty", "plaintext", "truncated-json", "invalid-utf8", "json-string", "json-number"],
)
def test_malformed_bodies_never_error(body, caplog):
    """A public unauthenticated endpoint must not 500 on garbage — a stack
    trace here would be a free error oracle."""
    with caplog.at_level(logging.WARNING, logger="app.api.v1.csp_report"):
        response = _post(body)

    assert response.status_code == 204
    assert caplog.records == []


def test_oversized_body_is_dropped_unparsed(caplog):
    """Log flooding: a megabyte of 'report' must not become a megabyte of log."""
    huge = json.dumps(
        {"csp-report": {"blocked-uri": "https://evil.example.com/" + "a" * MAX_REPORT_BYTES}}
    )
    assert len(huge) > MAX_REPORT_BYTES

    with caplog.at_level(logging.WARNING, logger="app.api.v1.csp_report"):
        response = _post(huge)

    assert response.status_code == 204
    assert caplog.records == []


def test_control_characters_are_stripped_from_logged_fields(caplog):
    """Log injection: report fields are attacker-controlled. A newline in
    `blocked-uri` would otherwise forge an entire log line — here, a fake
    'CSP violation' entry blaming a different document."""
    forged = "https://ok.example.com/\nWARNING:app.api.v1.csp_report:CSP violation: document=FORGED"
    with caplog.at_level(logging.WARNING, logger="app.api.v1.csp_report"):
        response = _post(json.dumps({"csp-report": {"blocked-uri": forged}}))

    assert response.status_code == 204
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "\n" not in message
    assert "FORGED" in message  # kept, but flattened onto the one real line


def test_long_field_is_truncated(caplog):
    """`blocked-uri` is legitimately a multi-KB `data:` URL sometimes."""
    long_uri = "data:image/png;base64," + "A" * (MAX_FIELD_CHARS * 3)
    with caplog.at_level(logging.WARNING, logger="app.api.v1.csp_report"):
        response = _post(json.dumps({"csp-report": {"blocked-uri": long_uri}}))

    assert response.status_code == 204
    message = caplog.records[0].getMessage()
    assert "…" in message
    assert len(message) < len(long_uri)


def test_report_batch_is_capped(caplog):
    """One request cannot produce an unbounded run of log lines."""
    payload = [
        {"type": "csp-violation", "body": {"blockedURL": f"https://evil.example.com/{i}"}}
        for i in range(50)
    ]
    with caplog.at_level(logging.WARNING, logger="app.api.v1.csp_report"):
        response = _post(json.dumps(payload), content_type="application/reports+json")

    assert response.status_code == 204
    assert len(caplog.records) == 10


def test_endpoint_is_absent_from_the_openapi_schema():
    """Publishing it would add a phantom operation to the generated frontend
    client. No API consumer calls this — only the browser does."""
    assert ENDPOINT not in live_app.openapi()["paths"]


def test_endpoint_requires_no_authentication():
    """The browser's reporting agent sends no credentials; an auth gate would
    silently discard every report."""
    response = _post(json.dumps(LEGACY_REPORT))
    assert response.status_code == 204


@pytest.mark.integration
def test_app_loggers_survive_the_alembic_migration_run(db_session):
    """Guards every log assertion in this file.

    `tests/conftest.py` runs `alembic command.upgrade()` **in-process**, and
    `alembic/env.py` configures logging via `fileConfig`. With the default
    `disable_existing_loggers=True` that call sets `disabled = True` on every
    already-imported `app.*` logger, silencing application logging for the
    rest of the session — which made the tests above fail only when run
    alongside any DB-backed test, and pass in isolation.

    Depends on `db_session` so the migration has definitely run first.
    """
    assert not logging.getLogger("app.api.v1.csp_report").disabled
    assert not logging.getLogger("app").disabled


@pytest.mark.rate_limit
@pytest.mark.skipif(
    not settings.rate_limit_enabled,
    reason="rate limiting disabled in this environment (E2E_DISABLE_RATE_LIMIT)",
)
def test_report_flood_is_rate_limited():
    """The only cap on total log volume from one source. Without it a single
    client can write to our logs without limit, which is both a disk-fill
    vector and a way to bury the real violations the watch window is for."""
    body = json.dumps({"csp-report": {"blocked-uri": "https://evil.example.com/x"}})
    with TestClient(live_app) as tc:
        codes = [
            tc.post(
                ENDPOINT, content=body, headers={"content-type": "application/csp-report"}
            ).status_code
            for _ in range(122)
        ]

    assert codes.count(204) == 120
    assert codes[120] == 429
