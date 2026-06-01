"""SEC-009: auth transactional emails must HTML-escape the user-controlled
``username`` (registration first name) in the HTML body, matching what the
order-email renderer already does.

The autouse ``captured_emails`` fixture swaps the ``send_*_email`` helpers for
stubs, so the real implementations are captured at import time (before any
fixture runs) and exercised here with only ``send_email`` patched to capture the
rendered HTML/text it would have sent.
"""

from __future__ import annotations

import pytest

from app.core import email

# Captured at import — before the autouse captured_emails fixture patches them.
_REAL_VERIFICATION = email.send_verification_email
_REAL_WELCOME = email.send_welcome_email
_REAL_PASSWORD_RESET = email.send_password_reset_email
_REAL_ACCOUNT_LOCKED = email.send_account_locked_email
_REAL_ALREADY_REGISTERED = email.send_already_registered_email

_XSS = '<img src=x onerror=alert(1)>"&'
_ESCAPED = "&lt;img src=x onerror=alert(1)&gt;"


@pytest.fixture
def captured_send(monkeypatch):
    """Capture the html/text ``send_email`` would receive (no Resend call)."""
    box: dict[str, str] = {}

    def _fake(*, email_to, subject, html_content, text_content=None):
        box["html"] = html_content
        box["text"] = text_content or ""
        return {"id": "stub"}

    monkeypatch.setattr(email, "send_email", _fake)
    return box


def test_verification_email_escapes_username_in_html(captured_send):
    _REAL_VERIFICATION(email_to="u@example.com", username=_XSS, token="tok")
    assert _XSS not in captured_send["html"]
    assert _ESCAPED in captured_send["html"]


def test_welcome_email_escapes_username_in_html(captured_send):
    _REAL_WELCOME(email_to="u@example.com", username=_XSS)
    assert _XSS not in captured_send["html"]
    assert _ESCAPED in captured_send["html"]


def test_password_reset_email_escapes_username_in_html(captured_send):
    _REAL_PASSWORD_RESET(email_to="u@example.com", username=_XSS, token="tok")
    assert _XSS not in captured_send["html"]
    assert _ESCAPED in captured_send["html"]


def test_account_locked_email_escapes_username_in_html(captured_send):
    _REAL_ACCOUNT_LOCKED(email_to="u@example.com", username=_XSS, lockout_minutes=30)
    assert _XSS not in captured_send["html"]
    assert _ESCAPED in captured_send["html"]


def test_already_registered_email_escapes_username_in_html(captured_send):
    _REAL_ALREADY_REGISTERED(email_to="u@example.com", username=_XSS)
    assert _XSS not in captured_send["html"]
    assert _ESCAPED in captured_send["html"]


def test_plain_text_part_is_not_escaped(captured_send):
    # The text/plain alternative is not HTML; escaping there would show literal
    # entities to the reader. It stays raw by design.
    _REAL_VERIFICATION(email_to="u@example.com", username="A & B", token="tok")
    assert "A & B" in captured_send["text"]
