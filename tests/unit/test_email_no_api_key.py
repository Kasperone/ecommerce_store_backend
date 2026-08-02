"""``send_email`` must not touch the network when Resend is not configured.

Dev, e2e and CI all run with ``RESEND_API_KEY=""``. Before this guard the SDK
still made a live call that Resend rejects with "API key is invalid", and its
retries cost roughly 100 s per email — a registration (verification + welcome)
measured **212 s** against a local backend, which reads as a hung server rather
than a misconfiguration.

The contract being pinned here is the documented one (`.claude/rules/security.md`):
with no key, the send is a no-op that does *not* raise, so registration still
succeeds and the account simply stays unverified.
"""

from __future__ import annotations

import pytest

from app.core import email as email_module

# Captured at import — the autouse ``captured_emails`` fixture swaps
# ``send_email`` for a stub, so the real implementation has to be grabbed
# before any fixture runs (same trick as ``test_auth_email_escape.py``).
_REAL_SEND = email_module.send_email


@pytest.fixture
def exploding_resend(monkeypatch):
    """Make any real Resend call fail loudly, so a leak can't pass silently."""
    calls: list[dict] = []

    def _boom(params):  # pragma: no cover -- the assertions require this to be unreachable
        calls.append(params)
        raise AssertionError("send_email reached the Resend SDK with no API key configured")

    monkeypatch.setattr(email_module.resend.Emails, "send", _boom)
    return calls


def test_send_email_is_a_noop_without_an_api_key(monkeypatch, exploding_resend):
    monkeypatch.setattr(email_module.settings, "RESEND_API_KEY", "")

    result = _REAL_SEND(
        email_to="nobody@example.com",
        subject="Verify your email",
        html_content="<p>hi</p>",
    )

    # Returns rather than raises: callers treat a raise as "delivery failed" and
    # some of them (abandoned-cart) un-arm state on it.
    assert result == {"id": "skipped-no-api-key"}
    assert exploding_resend == []


def test_send_email_still_calls_resend_when_configured(monkeypatch):
    sent: list[dict] = []

    def _capture(params):
        sent.append(params)
        return {"id": "sent-123"}

    monkeypatch.setattr(email_module.settings, "RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(email_module.resend.Emails, "send", _capture)

    result = _REAL_SEND(
        email_to="someone@example.com",
        subject="Verify your email",
        html_content="<p>hi</p>",
        text_content="hi",
    )

    assert result == {"id": "sent-123"}
    assert len(sent) == 1
    assert sent[0]["to"] == ["someone@example.com"]
    assert sent[0]["text"] == "hi"
