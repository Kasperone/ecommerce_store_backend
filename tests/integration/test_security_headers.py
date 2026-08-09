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

from app.core.security_headers import (
    CSP_REPORT_GROUP,
    SecurityHeadersMiddleware,
    build_csp,
)
from app.main import app as live_app

# These tests are pure middleware checks — no DB, no rate limiting, no
# integration mark.


def _build_app(
    *,
    hsts: bool,
    csp: str | None = None,
    csp_enforce: bool = False,
    csp_report_uri: str = "",
) -> FastAPI:
    isolated = FastAPI()
    isolated.add_middleware(
        SecurityHeadersMiddleware,
        hsts=hsts,
        csp=csp,
        csp_enforce=csp_enforce,
        csp_report_uri=csp_report_uri,
    )

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
    assert "strict-transport-security" not in {k.lower() for k in response.headers}


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
    assert "strict-transport-security" not in {k.lower() for k in response.headers}


def test_csp_absent_when_not_configured():
    """Backwards-compatibility: middleware without a CSP arg emits no
    Content-Security-Policy* header at all."""
    with TestClient(_build_app(hsts=False)) as tc:
        response = tc.get("/probe")
    lower_keys = {k.lower() for k in response.headers}
    assert "content-security-policy" not in lower_keys
    assert "content-security-policy-report-only" not in lower_keys


def test_csp_emitted_as_report_only_by_default():
    """Default CSP rollout uses Report-Only so any breakage shows up in
    browser consoles without breaking the live UI."""
    csp = build_csp()
    with TestClient(_build_app(hsts=False, csp=csp)) as tc:
        response = tc.get("/probe")
    assert response.headers.get("content-security-policy-report-only") == csp
    assert response.headers.get("content-security-policy") is None


def test_csp_enforced_when_csp_enforce_true():
    csp = build_csp()
    with TestClient(_build_app(hsts=False, csp=csp, csp_enforce=True)) as tc:
        response = tc.get("/probe")
    assert response.headers.get("content-security-policy") == csp
    assert response.headers.get("content-security-policy-report-only") is None


def test_build_csp_locks_down_default_directives():
    csp = build_csp()
    # Sanity: the policy locks frame-ancestors and base-uri, and limits
    # script/style sources to self (with the documented Tailwind escape).
    for directive in (
        "default-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: https://*.r2.cloudflarestorage.com",
    ):
        assert directive in csp


def test_build_csp_extends_connect_src_with_extra_origins():
    csp = build_csp(extra_connect_origins=["http://localhost:3000"])
    assert "connect-src 'self' http://localhost:3000" in csp


@pytest.mark.integration
def test_csp_report_only_present_on_live_app_health():
    """Live app wires CSP in Report-Only mode."""
    with TestClient(live_app) as tc:
        response = tc.get("/health")
    csp_ro = response.headers.get("content-security-policy-report-only")
    assert csp_ro is not None
    assert "default-src 'self'" in csp_ro
    assert response.headers.get("content-security-policy") is None


def test_build_csp_omits_reporting_directives_when_no_uri():
    """Reporting is opt-out: an empty CSP_REPORT_URI must leave the policy
    free of both directives rather than emitting a dangling group name."""
    csp = build_csp()
    assert "report-uri" not in csp
    assert "report-to" not in csp


def test_build_csp_emits_both_reporting_directives():
    """`report-uri` is deprecated but is all Safari implements; `report-to` is
    what Chrome wants. A policy carrying only one silently loses reports from
    whichever half of the browser market it omits."""
    csp = build_csp(report_uri="/api/v1/csp-report")
    assert "report-uri /api/v1/csp-report" in csp
    assert f"report-to {CSP_REPORT_GROUP}" in csp


def test_reporting_endpoints_header_binds_the_group_to_the_uri():
    """`report-to` names a group; without the Reporting-Endpoints header
    binding that name to a URL, the directive resolves to nothing."""
    csp = build_csp(report_uri="https://collector.example.com/csp")
    with TestClient(
        _build_app(hsts=False, csp=csp, csp_report_uri="https://collector.example.com/csp")
    ) as tc:
        response = tc.get("/probe")
    assert (
        response.headers.get("reporting-endpoints")
        == f'{CSP_REPORT_GROUP}="https://collector.example.com/csp"'
    )


def test_reporting_endpoints_header_absent_when_reporting_disabled():
    with TestClient(_build_app(hsts=False, csp=build_csp())) as tc:
        response = tc.get("/probe")
    assert "reporting-endpoints" not in {k.lower() for k in response.headers}


@pytest.mark.integration
def test_live_app_report_only_policy_actually_reports_somewhere():
    """The point of the whole module: a Report-Only policy blocks nothing, so
    one that also reports nowhere is inert decoration. The enforce-flip
    rollout depends on a watch window, which depends on this being wired."""
    with TestClient(live_app) as tc:
        response = tc.get("/health")
    csp_ro = response.headers.get("content-security-policy-report-only")
    assert csp_ro is not None
    assert "report-uri " in csp_ro
    assert f"report-to {CSP_REPORT_GROUP}" in csp_ro
    assert response.headers.get("reporting-endpoints") is not None
