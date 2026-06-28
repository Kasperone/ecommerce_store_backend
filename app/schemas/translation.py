"""Schemas for the admin Translations workspace (Phase 1).

Wire shapes for the registry-driven translation-management API
(``/api/v1/admin/translations``). All fields are plain JSON scalars/containers
so the frontend codegen stays clean (no ``Decimal``-style ``number|string``
drift). See :mod:`app.services.translation_registry` for the data model.
"""

from __future__ import annotations

from pydantic import Field

from app.schemas.base import BaseSchema


class TranslationFieldRow(BaseSchema):
    """One translatable field of a resource, with source + current translation."""

    key: str
    label_key: str
    source: str | None
    translation: str | None
    multiline: bool
    max_len: int | None
    # ``needs_translation`` = the source value is present (so a translation is
    # expected); ``translated`` = a non-empty translation exists for the locale.
    needs_translation: bool
    translated: bool


class TranslationResourceRow(BaseSchema):
    """A resource row in the workspace: its label + per-field translation state."""

    id: int
    label: str
    resource_type: str
    complete: bool
    # Completeness counts over *applicable* (source-present) fields.
    translated_count: int
    applicable_count: int
    fields: list[TranslationFieldRow]


class TranslationResourceListResponse(BaseSchema):
    """Paginated rows for one resource type + locale."""

    items: list[TranslationResourceRow]
    total: int
    page: int
    page_size: int
    pages: int
    locale: str
    resource_type: str


class TranslationResourceTypeSummary(BaseSchema):
    """Completeness rollup for one resource type in a locale."""

    type: str
    label_key: str
    resource_count: int
    complete_count: int
    incomplete_count: int


class TranslationResourceTypesResponse(BaseSchema):
    """Registry summary + per-type completeness counts for a locale."""

    locale: str
    types: list[TranslationResourceTypeSummary]
    resource_count: int
    complete_count: int
    incomplete_count: int


class TranslationUpdateRequest(BaseSchema):
    """Body for a translation update: ``{field_key: value}``.

    A blank/whitespace value clears the translation (stored as NULL), which the
    storefront then renders via the English fallback.
    """

    fields: dict[str, str | None] = Field(default_factory=dict)
