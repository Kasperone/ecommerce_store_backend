from pydantic import EmailStr, Field

from app.schemas.base import BaseSchema


class ContactRequest(BaseSchema):
    """Public contact-form submission.

    Bounds are generous but finite so a single request can't be abused to
    push a huge payload at the store inbox.
    """

    name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    subject: str = Field(..., min_length=1, max_length=200)
    message: str = Field(..., min_length=1, max_length=5000)


class ContactResponse(BaseSchema):
    """Acknowledgement returned regardless of whether the provider accepted
    the email (best-effort send)."""

    status: str = "sent"
