"""Integration tests for the public contact-form endpoint.

``POST /api/v1/contact`` validates the submission, forwards it to the store
inbox via Resend (best-effort), and is rate-limited 5/hour per submitter
email. The autouse ``captured_emails`` fixture patches ``send_email`` in
``app.core.email`` (kind ``"generic"``); the real ``send_contact_email`` calls
through to it, so we assert on that captured payload.
"""

import pytest

from app.core.config import settings

pytestmark = pytest.mark.integration


CONTACT_PATH = "/api/v1/contact"


def _valid_payload(**overrides) -> dict:
    payload = {
        "name": "Jane Buyer",
        "email": "jane@example.com",
        "subject": "Where is my order?",
        "message": "I placed an order last week and have not received it.",
    }
    payload.update(overrides)
    return payload


async def test_contact_sends_email_to_store_inbox(client, captured_emails, reset_limiter):
    response = await client.post(CONTACT_PATH, json=_valid_payload())

    assert response.status_code == 200
    assert response.json() == {"status": "sent"}

    recipient = settings.STORE_EMAIL or settings.EMAIL_FROM
    sent = [e for e in captured_emails if e["email_to"] == recipient]
    assert len(sent) == 1
    # The submission travels into the email body so staff can read + reply.
    assert "Where is my order?" in sent[0]["subject"]
    assert "jane@example.com" in sent[0]["html_content"]
    assert "I placed an order last week" in sent[0]["html_content"]


async def test_contact_uses_send_contact_email_helper(
    client, captured_emails, reset_limiter, monkeypatch
):
    """The endpoint must route through the patchable ``send_contact_email``
    helper (not assemble its own send), so the recipient + reply-to are
    surfaced to callers."""
    calls: list[dict] = []

    def _capture(**kwargs):
        calls.append(kwargs)
        return {"id": "mock-contact"}

    import app.api.v1.contact as contact_mod

    monkeypatch.setattr(contact_mod, "send_contact_email", _capture)

    response = await client.post(CONTACT_PATH, json=_valid_payload())

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["reply_to"] == "jane@example.com"
    assert calls[0]["name"] == "Jane Buyer"
    recipient = settings.STORE_EMAIL or settings.EMAIL_FROM
    assert calls[0]["email_to"] == recipient


async def test_contact_validation_rejects_blank_message(client, captured_emails, reset_limiter):
    response = await client.post(CONTACT_PATH, json=_valid_payload(message=""))
    assert response.status_code == 422
    assert captured_emails == []


async def test_contact_validation_rejects_bad_email(client, captured_emails, reset_limiter):
    response = await client.post(CONTACT_PATH, json=_valid_payload(email="not-an-email"))
    assert response.status_code == 422
    assert captured_emails == []


async def test_contact_provider_failure_does_not_500(
    client, captured_emails, reset_limiter, monkeypatch
):
    """A Resend failure is logged + swallowed — best-effort send, the
    submitter still gets a success response."""
    import app.api.v1.contact as contact_mod

    def _boom(**kwargs):
        raise RuntimeError("resend is down")

    monkeypatch.setattr(contact_mod, "send_contact_email", _boom)

    response = await client.post(CONTACT_PATH, json=_valid_payload())
    assert response.status_code == 200
    assert response.json() == {"status": "sent"}


@pytest.mark.rate_limit
async def test_contact_rate_limited_per_email(client, reset_limiter):
    """Five submissions from the same email exhaust the 5/hour budget; the
    sixth returns 429."""
    for i in range(5):
        r = await client.post(CONTACT_PATH, json=_valid_payload())
        assert r.status_code == 200, f"call {i} should pass the limiter"

    sixth = await client.post(CONTACT_PATH, json=_valid_payload())
    assert sixth.status_code == 429
