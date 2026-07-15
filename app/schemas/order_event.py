"""Schemas for the per-order timeline (order_events)."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from app.schemas.base import BaseSchema, TimestampSchema


class OrderEventActor(BaseSchema):
    """Minimal actor identity for a timeline row (None for system events)."""

    id: int
    email: str
    first_name: str
    last_name: str


class OrderEventResponse(TimestampSchema):
    """A single timeline entry as the admin API returns it.

    ``TimestampSchema`` supplies ``id`` / ``created_at`` / ``updated_at``.
    ``actor`` is null for system events (webhook payment, cron). ``payload``
    carries structured context per kind (e.g. ``{"from": "pending", "to":
    "paid"}``); the frontend renders a label from ``kind`` + ``payload``.
    """

    order_id: int
    kind: str
    comment: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    actor: OrderEventActor | None = None


class OrderEventCreate(BaseSchema):
    """Request body for POSTing a free-text staff comment to the timeline."""

    comment: str = Field(min_length=1, max_length=2000)

    @field_validator("comment")
    @classmethod
    def strip_comment(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Comment cannot be empty")
        return stripped
