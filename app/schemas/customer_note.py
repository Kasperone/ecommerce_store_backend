"""Admin customer-note schemas (internal CRM notes about a customer).

These power the note sub-routes under ``/api/v1/admin/customers/{id}/notes``.
"""

from typing import Annotated

from pydantic import Field

from app.schemas.base import BaseSchema, TimestampSchema

NOTE_BODY_MAX = 5000


class CustomerNoteCreate(BaseSchema):
    """Request body for creating a note."""

    body: Annotated[str, Field(min_length=1, max_length=NOTE_BODY_MAX)]


class CustomerNoteUpdate(BaseSchema):
    """Request body for editing a note's text."""

    body: Annotated[str, Field(min_length=1, max_length=NOTE_BODY_MAX)]


class CustomerNoteResponse(TimestampSchema):
    """A note with author attribution for display."""

    body: str
    author_id: int | None = None
    author_name: str
