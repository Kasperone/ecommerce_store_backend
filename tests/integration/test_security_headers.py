"""Integration tests for ``SecurityHeadersMiddleware``.

The production app wires the middleware once at import time with
``hsts=settings.ENVIRONMENT == "production"``. Tests run with
``ENVIRONMENT=development`` so the live ``app.main:app`` instance has
``hsts=False`` for its entire lifetime — to cover the production code
path we build a tiny throwaway app with ``hsts=True`` and assert against
its responses.

Use the synchronous ``starlette.testclient.TestClient`` here because the
goal is to inspect the response headers, not to exercise async DB code.
"""

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.core.security_headers import SecurityHeadersMiddleware
from app.main import app as live_app


# These tests are pure middleware checks — no DB, no rate limiting, no
# integration mark.


def _build_app(*, hsts: bool) -> FastAPI:
    isolated = FastAPI()
    isolated.add_middleware(SecurityHeadersMiddleware, hsts=hsts)

    @isolated.get("/probe")
    def _probe():
        return {"ok": True}

    return isolated


def test_always_on_headers_set_on_every_response():
    """X-Content-Type-Options, X-Frame-Options and Referrer-Policy must be
    set regardless of environment."""
    with TestClient(_build_app(hsts=False)) as tc:
        response = tc.get("/probe")
    assert response.status_code == 200
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


def test_hsts_absent_when_hsts_false():
    """In non-production environments HSTS must NOT be set — promoting a
    dev/staging origin to HSTS by mistake would lock browsers out of
    plain-HTTP localhost for a year."""
    with TestClient(_build_app(hsts=False)) as tc:
        response = tc.get("/probe")
    assert "strict-transport-security" not in {
        k.lower() for k in response.headers.keys()
    }


def test_hsts_set_when_hsts_true():
    """Production wires hsts=True and the middleware then emits a 1y HSTS
    header with includeSubDomains."""
    with TestClient(_build_app(hsts=True)) as tc:
        response = tc.get("/probe")
    hsts_value = response.headers.get("strict-transport-security")
    assert hsts_value is not None
    assert "max-age=31536000" in hsts_value
    assert "includeSubDomains" in hsts_value


@pytest.mark.integration
def test_always_on_headers_present_on_live_app_health():
    """End-to-end sanity: the always-on headers reach a real route on the
    actually-wired ``app.main:app`` instance, not just the throwaway one
    used by the contract tests above. ``ENVIRONMENT=development`` means
    HSTS is correctly absent here."""
    with TestClient(live_app) as tc:
        response = tc.get("/health")
    assert response.status_code == 200
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert "strict-transport-security" not in {
        k.lower() for k in response.headers.keys()
    }
