"""Tests for the store brand-name resolver (app/services/brand.py).

``get_brand_name`` opens its own short-lived ``SessionLocal`` (so email/feed
call sites need no ``db`` param), which is invisible to the test-transaction
savepoint — so the DB interaction is stubbed at the module seams
(``SessionLocal`` / ``crud_store_config``) rather than exercised against the
shared fixture session.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import brand as brand_module
from app.services.brand import get_brand_name, get_email_brand_name


class _StubSession:
    """Minimal stand-in for the resolver's private session."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def stub_session(monkeypatch: pytest.MonkeyPatch) -> _StubSession:
    session = _StubSession()
    monkeypatch.setattr(brand_module, "SessionLocal", lambda: session)
    return session


def _stub_store_name(
    monkeypatch: pytest.MonkeyPatch, store_name: str, email_from_name: str = ""
) -> None:
    monkeypatch.setattr(
        brand_module.crud_store_config,
        "get_singleton",
        lambda db: SimpleNamespace(store_name=store_name, email_from_name=email_from_name),
    )


def test_returns_store_name_when_set(monkeypatch, stub_session):
    _stub_store_name(monkeypatch, "Sklep Testowy")
    assert get_brand_name() == "Sklep Testowy"
    assert stub_session.closed


def test_falls_back_to_email_from_name_when_blank(monkeypatch, stub_session):
    _stub_store_name(monkeypatch, "   ")
    assert get_brand_name() == settings.EMAIL_FROM_NAME


def test_custom_fallback_used_over_env(monkeypatch, stub_session):
    _stub_store_name(monkeypatch, "")
    assert get_brand_name(fallback="Feed Title") == "Feed Title"


def test_store_name_wins_over_custom_fallback(monkeypatch, stub_session):
    _stub_store_name(monkeypatch, "Configured Brand")
    assert get_brand_name(fallback="Feed Title") == "Configured Brand"


# ---------------------------------------------------------------------------
# get_email_brand_name — email_from_name → store_name → env (B1)
# ---------------------------------------------------------------------------


def test_email_brand_prefers_dedicated_field(monkeypatch, stub_session):
    _stub_store_name(monkeypatch, "Store Brand", email_from_name="Sender Brand")
    assert get_email_brand_name() == "Sender Brand"
    # …while the generic (feed) resolver keeps ignoring it:
    assert get_brand_name() == "Store Brand"


def test_email_brand_falls_back_to_store_name(monkeypatch, stub_session):
    _stub_store_name(monkeypatch, "Store Brand", email_from_name="  ")
    assert get_email_brand_name() == "Store Brand"


def test_email_brand_falls_back_to_env(monkeypatch, stub_session):
    _stub_store_name(monkeypatch, "", email_from_name="")
    assert get_email_brand_name() == settings.EMAIL_FROM_NAME


def test_email_brand_fail_open(monkeypatch):
    def _boom() -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(brand_module, "SessionLocal", _boom)
    assert get_email_brand_name() == settings.EMAIL_FROM_NAME


def test_fail_open_when_db_unavailable(monkeypatch):
    def _boom() -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(brand_module, "SessionLocal", _boom)
    assert get_brand_name() == settings.EMAIL_FROM_NAME


def test_fail_open_closes_nothing_but_lookup_error_still_falls_back(monkeypatch, stub_session):
    def _raise(db):
        raise RuntimeError("query failed")

    monkeypatch.setattr(brand_module.crud_store_config, "get_singleton", _raise)
    assert get_brand_name() == settings.EMAIL_FROM_NAME
    assert stub_session.closed


# ---------------------------------------------------------------------------
# Representative end-to-end: configured store_name flows into a real email
# ---------------------------------------------------------------------------

# Captured at import — before the autouse captured_emails fixture patches them
# (same pattern as tests/unit/test_auth_email_escape.py).
from app.core import email as email_module  # noqa: E402

_REAL_VERIFICATION = email_module.send_verification_email


def test_verification_email_uses_configured_store_name(monkeypatch, stub_session):
    _stub_store_name(monkeypatch, "Sklep Kaspra")

    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return {"id": "test"}

    monkeypatch.setattr(email_module, "send_email", _capture)
    _REAL_VERIFICATION(email_to="x@example.com", username="X", token="tok")

    assert captured["subject"].startswith("Sklep Kaspra")
    assert "Sklep Kaspra" in captured["html_content"]
    assert settings.EMAIL_FROM_NAME not in captured["subject"] or (
        settings.EMAIL_FROM_NAME == "Sklep Kaspra"
    )
