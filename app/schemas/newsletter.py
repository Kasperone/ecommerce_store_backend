"""Pydantic schemas for the public newsletter subscribe endpoint.

Anti-enumeration posture (mirrors ``/auth/password-reset/request``): the
response is a fixed ``{"status": "subscribed"}`` whether the email is new,
already subscribed, or reactivated — the endpoint never reveals prior state.
"""

from __future__ import annotations

from pydantic import EmailStr

from app.schemas.base import BaseSchema


class NewsletterSubscribeRequest(BaseSchema):
    """Request body for ``POST /api/v1/newsletter/subscribe``."""

    email: EmailStr


class NewsletterSubscribeResponse(BaseSchema):
    """Generic, state-independent success response (anti-enumeration)."""

    status: str


class NewsletterUnsubscribeResponse(BaseSchema):
    """One-click unsubscribe result.

    Also state-independent, and for a stronger reason than anti-enumeration:
    the caller is usually a mailbox provider acting on the recipient's behalf
    (RFC 8058), and surfacing a failure to it would show the recipient
    "unsubscribe didn't work" — sending them to the spam button instead.
    """

    status: str
