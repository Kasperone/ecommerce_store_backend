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
from app.models.translation_meta import TranslationMeta
from app.services.translation_registry import (
    TRANSLATABLE_RESOURCES,
    TRANSLATION_LOCALES,
    TranslatableResourceDef,
    field_is_translated,
    field_needs_translation,
    get_resource_def,
    resource_completeness,
    resource_is_complete,
    source_digest,
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


def get_field_digests(
    db: Session, *, resource_type: str, resource_id: int, locale: str
) -> dict[str, str]:
    """Stored source-digests for one resource: ``{field_key: digest}``."""
    rows = (
        db.query(TranslationMeta)
        .filter(
            TranslationMeta.resource_type == resource_type,
            TranslationMeta.resource_id == resource_id,
            TranslationMeta.locale == locale,
        )
        .all()
    )
    return {r.field_key: r.source_digest for r in rows}


def get_field_digests_bulk(
    db: Session, *, resource_type: str, resource_ids: list[int], locale: str
) -> dict[tuple[int, str], str]:
    """Stored digests for many resources: ``{(resource_id, field_key): digest}``."""
    if not resource_ids:
        return {}
    rows = (
        db.query(TranslationMeta)
        .filter(
            TranslationMeta.resource_type == resource_type,
            TranslationMeta.resource_id.in_(resource_ids),
            TranslationMeta.locale == locale,
        )
        .all()
    )
    return {(r.resource_id, r.field_key): r.source_digest for r in rows}


def build_resource_row(
    instance: BaseModel,
    resource_def: TranslatableResourceDef,
    locale: str,
    *,
    digests: dict[str, str] | None = None,
) -> dict:
    """Serialise one resource into the workspace row shape for ``locale``.

    ``digests`` maps ``field_key -> stored source-digest`` for this resource; a
    field is *outdated* when it has a translation, a stored digest exists, and
    that digest no longer matches the live source. A missing digest means
    "unknown" (legacy) and is treated as not outdated.
    """
    digests = digests or {}
    fields = []
    has_outdated = False
    for f in resource_def.fields:
        attr = f.attr_for(locale)
        source = getattr(instance, f.en_attr, None)
        translation = getattr(instance, attr, None) if attr is not None else None
        translated = field_is_translated(instance, f, locale)
        stored = digests.get(f.key)
        outdated = bool(translated and stored is not None and stored != source_digest(source))
        if outdated:
            has_outdated = True
        fields.append(
            {
                "key": f.key,
                "label_key": f.label_key,
                "source": source,
                "translation": translation,
                "multiline": f.multiline,
                "max_len": f.max_len,
                "needs_translation": field_needs_translation(instance, f),
                "translated": translated,
                "outdated": outdated,
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
        "has_outdated": has_outdated,
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

    ``status='missing'`` keeps only incomplete resources; ``status='outdated'``
    keeps only resources with a stale translation. ``q`` narrows by a
    case-insensitive match on the label column. Completeness/outdated filtering
    and pagination happen in Python (see module docstring).
    """
    label_col = getattr(resource_def.model, resource_def.label_attr)
    query = _active_query(db, resource_def)
    if q:
        query = query.filter(label_col.ilike(f"%{q}%"))
    query = query.order_by(label_col.asc())

    instances = query.all()
    digests = get_field_digests_bulk(
        db,
        resource_type=resource_def.type,
        resource_ids=[inst.id for inst in instances],
        locale=locale,
    )
    rows = [
        build_resource_row(
            inst,
            resource_def,
            locale,
            digests={key: digest for (rid, key), digest in digests.items() if rid == inst.id},
        )
        for inst in instances
    ]
    if status == "missing":
        rows = [r for r in rows if not r["complete"]]
    elif status == "outdated":
        rows = [r for r in rows if r["has_outdated"]]

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

    Only fields whose value actually changes are touched: a changed non-empty
    field gets its source-digest (re)stamped to the *current* English source
    (clearing any outdated flag — the admin just reconciled it); a cleared field
    drops its digest row. Fields left unchanged keep their stored digest, so an
    outdated flag survives an unrelated edit to a sibling field.
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

        if normalized == getattr(instance, attr):
            continue  # no change — preserve any existing digest/outdated state
        setattr(instance, attr, normalized)
        if normalized is None:
            _clear_digest(db, resource_def.type, resource_id, key, locale)
        else:
            _stamp_digest(
                db,
                resource_def.type,
                resource_id,
                key,
                locale,
                source_digest(getattr(instance, field.en_attr)),
            )
    return instance


def _stamp_digest(
    db: Session,
    resource_type: str,
    resource_id: int,
    field_key: str,
    locale: str,
    digest: str,
) -> None:
    """Upsert the source-digest for a just-saved translation field."""
    row = (
        db.query(TranslationMeta)
        .filter(
            TranslationMeta.resource_type == resource_type,
            TranslationMeta.resource_id == resource_id,
            TranslationMeta.field_key == field_key,
            TranslationMeta.locale == locale,
        )
        .first()
    )
    if row is None:
        db.add(
            TranslationMeta(
                resource_type=resource_type,
                resource_id=resource_id,
                field_key=field_key,
                locale=locale,
                source_digest=digest,
            )
        )
    else:
        row.source_digest = digest


def _clear_digest(
    db: Session,
    resource_type: str,
    resource_id: int,
    field_key: str,
    locale: str,
) -> None:
    """Drop the source-digest row for a cleared translation field."""
    db.query(TranslationMeta).filter(
        TranslationMeta.resource_type == resource_type,
        TranslationMeta.resource_id == resource_id,
        TranslationMeta.field_key == field_key,
        TranslationMeta.locale == locale,
    ).delete()


__all__ = [
    "FieldTooLongError",
    "UnknownFieldError",
    "UnsupportedLocaleError",
    "build_resource_row",
    "get_field_digests",
    "get_resource_def",
    "list_resources",
    "summarize_types",
    "update_resource",
    "validate_locale",
]
