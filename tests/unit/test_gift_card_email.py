"""Unit tests for the gift-card issue email builder (pure — no transport).

Focus on the v2 addition: the optional buyer gift message renders (escaped) in
both bodies when present and is absent otherwise.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.gift_card_email import build_gift_card_email


def test_message_renders_in_both_bodies_when_present():
    _, html, text = build_gift_card_email(
        code="ABCD-EFGH-JKLM-NPQR",
        amount=Decimal("50.00"),
        currency="USD",
        expires_at=None,
        message="Enjoy the shopping spree!",
        lang="en",
    )
    assert "Enjoy the shopping spree!" in html
    assert "Enjoy the shopping spree!" in text
    assert "A message for you" in html


def test_message_absent_when_blank():
    _, html, text = build_gift_card_email(
        code="ABCD-EFGH-JKLM-NPQR",
        amount=Decimal("50.00"),
        currency="USD",
        expires_at=None,
        message="   ",
        lang="en",
    )
    assert "A message for you" not in html
    assert "A message for you" not in text


def test_message_is_html_escaped():
    _, html, _ = build_gift_card_email(
        code="ABCD-EFGH-JKLM-NPQR",
        amount=Decimal("50.00"),
        currency="USD",
        expires_at=None,
        message="<script>alert(1)</script>",
        lang="pl",
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    # PL locale label.
    assert "Wiadomość dla Ciebie" in html
