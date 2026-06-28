"""Registry-driven service for the admin Translations workspace (Phase 1).

Generic list / summarise / update over the translatable-resource registry
(:mod:`app.services.translation_registry`) so there is no per-model endpoint
duplication. Reads and writes the per-locale ``*_pl`` columns directly (Path A).

Completeness is computed in Python (it depends on source-present semantics, see
the registry), so listing loads the active rows of a type and filters/paginates
in memory. That is fine for the small admin-facing tables; the heaviest type
(products) is narrowed by the ``q`` search. A SQL-side optimisation (CASE-based
counts) can land later if a catalogue grows large enough to need it.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.base import BaseModel
from app.services.translation_registry import (
    TRANSLATABLE_RESOURCES,
    TRANSLATION_LOCALES,
    TranslatableResourceDef,
    field_is_translated,
    field_needs_translation,
    get_resource_def,
    resource_completeness,
    resource_is_complete,
)


class UnsupportedLocaleError(ValueError):
    """Raised when a locale is not an exposed translation locale."""


class UnknownFieldError(ValueError):
    """Raised when an update names a field not on the resource type."""


class FieldTooLongError(ValueError):
    """Raised when a translation value exceeds the field's max length."""

    def __init__(self, key: str, max_len: int) -> None:
        super().__init__(f"Field {key!r} exceeds max length {max_len}")
        self.key = key
        self.max_len = max_len


def validate_locale(locale: str) -> None:
    """Raise :class:`UnsupportedLocaleError` if ``locale`` is not exposed."""
    if locale not in TRANSLATION_LOCALES:
        supported = ", ".join(TRANSLATION_LOCALES)
        raise UnsupportedLocaleError(f"Unsupported locale {locale!r}; supported: {supported}")


def _active_query(db: Session, resource_def: TranslatableResourceDef):
    query = db.query(resource_def.model)
    if resource_def.active_attr is not None:
        active_col = getattr(resource_def.model, resource_def.active_attr)
        query = query.filter(active_col.is_(True))
    return query


def _label(instance: BaseModel, resource_def: TranslatableResourceDef) -> str:
    label = getattr(instance, resource_def.label_attr, "") or f"#{instance.id}"
    return str(label)


def build_resource_row(
    instance: BaseModel, resource_def: TranslatableResourceDef, locale: str
) -> dict:
    """Serialise one resource into the workspace row shape for ``locale``."""
    fields = []
    for f in resource_def.fields:
        attr = f.attr_for(locale)
        source = getattr(instance, f.en_attr, None)
        translation = getattr(instance, attr, None) if attr is not None else None
        fields.append(
            {
                "key": f.key,
                "label_key": f.label_key,
                "source": source,
                "translation": translation,
                "multiline": f.multiline,
                "max_len": f.max_len,
                "needs_translation": field_needs_translation(instance, f),
                "translated": field_is_translated(instance, f, locale),
            }
        )
    translated_count, applicable_count = resource_completeness(instance, resource_def, locale)
    return {
        "id": instance.id,
        "label": _label(instance, resource_def),
        "resource_type": resource_def.type,
        "complete": resource_is_complete(instance, resource_def, locale),
        "translated_count": translated_count,
        "applicable_count": applicable_count,
        "fields": fields,
    }


def list_resources(
    db: Session,
    *,
    resource_def: TranslatableResourceDef,
    locale: str,
    status: str = "all",
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """Return ``(rows, total)`` for one resource type + locale.

    ``status='missing'`` keeps only incomplete resources. ``q`` narrows by a
    case-insensitive match on the label column. Completeness filtering and
    pagination happen in Python (see module docstring).
    """
    query = _active_query(db, resource_def)
    if q:
        label_col = getattr(resource_def.model, resource_def.label_attr)
        query = query.filter(label_col.ilike(f"%{q}%"))
    label_col = getattr(resource_def.model, resource_def.label_attr)
    query = query.order_by(label_col.asc())

    rows = [build_resource_row(inst, resource_def, locale) for inst in query.all()]
    if status == "missing":
        rows = [r for r in rows if not r["complete"]]

    total = len(rows)
    skip = (page - 1) * page_size
    return rows[skip : skip + page_size], total


def summarize_types(db: Session, locale: str) -> dict:
    """Per-type + overall completeness counts over active resources for ``locale``."""
    type_summaries: list[dict] = []
    resource_count = 0
    complete_count = 0
    for resource_def in TRANSLATABLE_RESOURCES:
        instances = _active_query(db, resource_def).all()
        count = len(instances)
        complete = sum(1 for inst in instances if resource_is_complete(inst, resource_def, locale))
        type_summaries.append(
            {
                "type": resource_def.type,
                "label_key": resource_def.label_key,
                "resource_count": count,
                "complete_count": complete,
                "incomplete_count": count - complete,
            }
        )
        resource_count += count
        complete_count += complete
    return {
        "locale": locale,
        "types": type_summaries,
        "resource_count": resource_count,
        "complete_count": complete_count,
        "incomplete_count": resource_count - complete_count,
    }


def update_resource(
    db: Session,
    *,
    resource_def: TranslatableResourceDef,
    resource_id: int,
    locale: str,
    fields: dict[str, str | None],
) -> BaseModel | None:
    """Apply translation edits to one resource's ``*_pl`` columns for ``locale``.

    Returns the (un-committed) instance for the route to audit + commit, or
    ``None`` when the resource id does not exist. Raises
    :class:`UnknownFieldError` / :class:`FieldTooLongError` /
    :class:`UnsupportedLocaleError` on bad input. A blank value clears the
    translation (stored NULL → storefront falls back to English).
    """
    validate_locale(locale)
    instance = db.get(resource_def.model, resource_id)
    if instance is None:
        return None

    field_map = {f.key: f for f in resource_def.fields}
    for key, value in fields.items():
        field = field_map.get(key)
        if field is None:
            raise UnknownFieldError(
                f"Unknown field {key!r} for resource type {resource_def.type!r}"
            )
        attr = field.attr_for(locale)
        if attr is None:
            raise UnsupportedLocaleError(f"Field {key!r} has no column for locale {locale!r}")
        normalized = value.strip() if isinstance(value, str) else value
        if not normalized:
            normalized = None
        elif field.max_len is not None and len(normalized) > field.max_len:
            raise FieldTooLongError(key, field.max_len)
        setattr(instance, attr, normalized)
    return instance


__all__ = [
    "FieldTooLongError",
    "UnknownFieldError",
    "UnsupportedLocaleError",
    "build_resource_row",
    "get_resource_def",
    "list_resources",
    "summarize_types",
    "update_resource",
    "validate_locale",
]
