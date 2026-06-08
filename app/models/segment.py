"""Customer segment model — a stored, named, typed-filter customer predicate.

A segment is a saved definition (name + description + a closed typed-JSON
``filters`` predicate). Membership is **computed live at resolve time** — there
is deliberately **no join table**: segments are dynamic, the app has no recompute
scheduler, and the customer population is small. This is the opposite of Wave-2
collections (which materialize membership) — and that is correct: segments want
live membership.

The ``filters`` column is plain JSON; its shape is the single source of truth in
``app.schemas.segment.SegmentFilters`` (Pydantic, ``extra="forbid"``), which
validates writes **and** drives the set-based SQL resolver in
``app.crud.segment``. There is no query language and no ``eval`` — only a fixed
closed vocabulary of optional, AND-combined predicates.
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class Segment(BaseModel):
    """A saved customer segment definition.

    ``name`` is required; ``description`` is optional. ``filters`` is a JSON dict
    matching ``SegmentFilters`` (validated at the API boundary). ``is_active`` is
    a soft on/off toggle (defaults True) — it does not affect resolution, only
    surfaces intent in the admin UI / campaign picker.
    """

    __tablename__ = "segments"

    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(255))
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    def __repr__(self) -> str:
        return f"<Segment {self.id} {self.name!r}>"
