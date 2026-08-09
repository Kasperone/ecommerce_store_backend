"""Tests for production gating of `/docs`, `/redoc` and `/openapi.json`.

`app.main:app` is built once at import time from the ambient settings, so the
live app cannot be re-parameterised. The env-derivation logic is therefore
tested on `Settings` directly, and the wiring is tested by building a throwaway
FastAPI the same way `main.py` does.
"""

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.core.config import Settings
from app.main import app as live_app

REQUIRED_SECRETS = {
    "SECRET_KEY": "test-secret-key-that-is-long-enough-x",
    "JWT_SECRET_KEY": "test-jwt-secret-key-that-is-long-x",
}


def _settings(**overrides) -> Settings:
    return Settings(**{**REQUIRED_SECRETS, **overrides})  # pyright: ignore[reportCallIssue]


def _build_app(settings: Settings) -> FastAPI:
    """Build exactly the way `app/main.py` does — by spreading
    `settings.api_docs_urls`, not by restating the mapping. A test that
    restated it would pass while the real wiring drifted."""
    return FastAPI(**settings.api_docs_urls)


@pytest.mark.parametrize("environment", ["development", "staging"])
def test_docs_enabled_outside_production(environment):
    """Dev and staging keep Swagger — it is a working tool there, and CI runs
    its backend containers with ENVIRONMENT=development, so codegen-check and
    e2e must not need an explicit opt-in."""
    assert _settings(ENVIRONMENT=environment).api_docs_enabled is True


def test_docs_disabled_in_production_by_default():
    """The whole point: production must not serve the API map or Swagger's
    CSP-violating HTML unless someone opts in deliberately."""
    assert _settings(ENVIRONMENT="production", DEBUG=False).api_docs_enabled is False


def test_explicit_override_wins_in_both_directions():
    """`ENABLE_API_DOCS` is an escape hatch, not a one-way switch — an internal
    prod deploy behind a VPN may legitimately want docs, and a staging box
    exposed to the internet may legitimately not."""
    assert (
        _settings(ENVIRONMENT="production", DEBUG=False, ENABLE_API_DOCS=True).api_docs_enabled
        is True
    )
    assert _settings(ENVIRONMENT="development", ENABLE_API_DOCS=False).api_docs_enabled is False


def test_all_three_routes_disappear_together_in_production():
    """Hiding `/docs` while still serving `/openapi.json` is theatre — the
    schema is the disclosure, and anyone can point a local Swagger at it. They
    must move as one."""
    prod = _settings(ENVIRONMENT="production", DEBUG=False)
    with TestClient(_build_app(prod)) as tc:
        assert tc.get("/docs").status_code == 404
        assert tc.get("/redoc").status_code == 404
        assert tc.get("/openapi.json").status_code == 404


def test_all_three_routes_served_outside_production():
    with TestClient(_build_app(_settings(ENVIRONMENT="development"))) as tc:
        assert tc.get("/docs").status_code == 200
        assert tc.get("/redoc").status_code == 200
        assert tc.get("/openapi.json").status_code == 200


@pytest.mark.integration
def test_live_app_serves_docs_under_the_test_environment():
    """Sanity that the wiring did not disable docs everywhere — the test env is
    `development`, so the live app must still serve them. This is also what
    protects `npm run gen:types` and CI codegen-check from this change."""
    with TestClient(live_app) as tc:
        assert tc.get("/openapi.json").status_code == 200
        assert tc.get("/docs").status_code == 200
