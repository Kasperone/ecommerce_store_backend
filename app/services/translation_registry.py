"""Declarative registry of translatable resources (Path A — over per-locale columns).

Single source of truth that maps a translatable resource *type* to its model and
its translatable fields. Each field names the ORM attribute holding the English
**source** value plus, per locale, the attribute holding that locale's
translation. The registry drives the completeness audit
(``app.scripts.audit_translations``) and — once Phase 1 lands — the admin
Translations API, so adding a translatable field later is a one-line edit here.

Background and the Shopify model this ports (``TranslatableResource`` /
``TranslatableContent``, fall-back-to-source semantics, digest-based outdated
detection): see
``.claude/notes/plans/i18n/in-progress/2026-06-28-shopify-style-translation-management.md``.

Locale model
------------
English is the **source** locale (default, unprefixed in URLs). The only
translation locale today is Polish (``pl``), stored in the ``*_pl`` columns. The
helpers are locale-parametrised (they take a ``locale`` and resolve the column
via :attr:`TranslatableField.locale_attrs`), so adding a locale later is a
registry edit plus the columns — or the Path B generic-translations migration —
not a rewrite.

Completeness semantics
----------------------
A field only *needs* a translation when its source value is non-empty — there is
nothing to translate for a blank optional field (e.g. a method with no English
``description``). A resource is **complete** for a locale when every
source-present field has a non-empty translation. This mirrors Shopify, where the
source falls through when a translation is absent, and avoids flagging
empty-source fields as false gaps.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.models.base import BaseModel
from app.models.category import Category
from app.models.collection import Collection
from app.models.product import Product
from app.models.shipping import ShippingMethod

# Translation locales we expose. The source locale ``en`` is implicit and never
# listed here. Only ``pl`` exists today; adding a locale means a column per field
# (or the Path B migration) plus extending each field's ``locale_attrs``.
SOURCE_LOCALE = "en"
TRANSLATION_LOCALES: tuple[str, ...] = ("pl",)


@dataclass(frozen=True)
class TranslatableField:
    """One translatable field on a resource.

    ``en_attr`` is the ORM attribute holding the source (English) value;
    ``locale_attrs`` maps a translation locale to the ORM attribute holding that
    locale's value. ``max_len`` mirrors the column constraint so the Phase 1 API
    and frontend can validate before hitting the DB CHECK.
    """

    key: str
    en_attr: str
    locale_attrs: dict[str, str]
    label_key: str
    multiline: bool = False
    max_len: int | None = None

    def attr_for(self, locale: str) -> str | None:
        """ORM attribute holding this field's value for ``locale`` (None if unsupported)."""
        return self.locale_attrs.get(locale)


@dataclass(frozen=True)
class TranslatableResourceDef:
    """A translatable resource type: its model, fields, and how to list/label it."""

    type: str
    model: type[BaseModel]
    fields: tuple[TranslatableField, ...]
    label_attr: str = "name"
    # Column used to scope an audit/list to live rows (``is_active``). None = no
    # active flag; scan every row.
    active_attr: str | None = "is_active"
    # i18n key for the resource type's display name in the admin workspace.
    label_key: str = ""


# Shared SEO field set (Product / Category / Collection share these column names
# and length limits).
def _seo_fields() -> tuple[TranslatableField, ...]:
    return (
        TranslatableField(
            key="meta_title",
            en_attr="meta_title",
            locale_attrs={"pl": "meta_title_pl"},
            label_key="admin.translations.fields.metaTitle",
            max_len=70,
        ),
        TranslatableField(
            key="meta_description",
            en_attr="meta_description",
            locale_attrs={"pl": "meta_description_pl"},
            label_key="admin.translations.fields.metaDescription",
            multiline=True,
            max_len=160,
        ),
    )


# NOTE on excluded fields: ``slug_pl`` (Category/Collection) is translatable but
# also a routable URL key with a partial-unique index — deferred (spec D5) to
# avoid slug-collision handling in the first cut. Product/variant-option
# translations widen the registry later (spec D4) once the flat-resource pattern
# is proven; they are additive entries here when the time comes.
TRANSLATABLE_RESOURCES: tuple[TranslatableResourceDef, ...] = (
    TranslatableResourceDef(
        type="shipping_method",
        model=ShippingMethod,
        label_key="admin.translations.resources.shippingMethod",
        fields=(
            TranslatableField(
                key="name",
                en_attr="name",
                locale_attrs={"pl": "name_pl"},
                label_key="admin.translations.fields.name",
                max_len=80,
            ),
            TranslatableField(
                key="description",
                en_attr="description",
                locale_attrs={"pl": "description_pl"},
                label_key="admin.translations.fields.description",
                multiline=True,
            ),
        ),
    ),
    TranslatableResourceDef(
        type="product",
        model=Product,
        label_key="admin.translations.resources.product",
        fields=(
            TranslatableField(
                key="name",
                en_attr="name",
                locale_attrs={"pl": "name_pl"},
                label_key="admin.translations.fields.name",
                max_len=200,
            ),
            TranslatableField(
                key="description",
                en_attr="description",
                locale_attrs={"pl": "description_pl"},
                label_key="admin.translations.fields.description",
                multiline=True,
            ),
            *_seo_fields(),
        ),
    ),
    TranslatableResourceDef(
        type="category",
        model=Category,
        label_key="admin.translations.resources.category",
        fields=(
            TranslatableField(
                key="name",
                en_attr="name",
                locale_attrs={"pl": "name_pl"},
                label_key="admin.translations.fields.name",
                max_len=100,
            ),
            TranslatableField(
                key="description",
                en_attr="description",
                locale_attrs={"pl": "description_pl"},
                label_key="admin.translations.fields.description",
                multiline=True,
            ),
            *_seo_fields(),
        ),
    ),
    TranslatableResourceDef(
        type="collection",
        model=Collection,
        label_key="admin.translations.resources.collection",
        fields=(
            TranslatableField(
                key="name",
                en_attr="name",
                locale_attrs={"pl": "name_pl"},
                label_key="admin.translations.fields.name",
                max_len=100,
            ),
            TranslatableField(
                key="description",
                en_attr="description",
                locale_attrs={"pl": "description_pl"},
                label_key="admin.translations.fields.description",
                multiline=True,
            ),
            *_seo_fields(),
        ),
    ),
)

RESOURCES_BY_TYPE: dict[str, TranslatableResourceDef] = {r.type: r for r in TRANSLATABLE_RESOURCES}


def get_resource_def(resource_type: str) -> TranslatableResourceDef:
    """Look up a resource def by type, raising ``KeyError`` with a clear message."""
    try:
        return RESOURCES_BY_TYPE[resource_type]
    except KeyError:
        known = ", ".join(sorted(RESOURCES_BY_TYPE))
        raise KeyError(
            f"Unknown translatable resource type {resource_type!r}; known: {known}"
        ) from None


def source_digest(value: object) -> str:
    """SHA-256 (hex) of a field's source value, used for outdated detection.

    A blank/None source normalises to the empty string so its digest is stable.
    Mirrors Shopify's ``translatableContentDigest``: stored when a translation is
    saved, compared against the live source's digest to detect a stale source.
    """
    text = "" if value is None else str(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _value_present(value: object) -> bool:
    """True when ``value`` is a non-empty (after-strip) string."""
    return value is not None and bool(str(value).strip())


def field_needs_translation(instance: BaseModel, field: TranslatableField) -> bool:
    """True when ``field``'s source value is present (so a translation is expected)."""
    return _value_present(getattr(instance, field.en_attr, None))


def field_is_translated(instance: BaseModel, field: TranslatableField, locale: str) -> bool:
    """True when ``field`` has a non-empty translation for ``locale``."""
    attr = field.attr_for(locale)
    if attr is None:
        return False
    return _value_present(getattr(instance, attr, None))


def missing_fields(
    instance: BaseModel, resource_def: TranslatableResourceDef, locale: str
) -> list[TranslatableField]:
    """Source-present fields lacking a translation for ``locale``."""
    return [
        f
        for f in resource_def.fields
        if field_needs_translation(instance, f) and not field_is_translated(instance, f, locale)
    ]


def resource_completeness(
    instance: BaseModel, resource_def: TranslatableResourceDef, locale: str
) -> tuple[int, int]:
    """Return ``(translated, applicable)`` counts for ``instance`` in ``locale``.

    ``applicable`` counts only source-present fields (a blank optional source
    needs no translation); ``translated`` counts those that have a translation.
    """
    applicable = [f for f in resource_def.fields if field_needs_translation(instance, f)]
    translated = sum(1 for f in applicable if field_is_translated(instance, f, locale))
    return translated, len(applicable)


def resource_is_complete(
    instance: BaseModel, resource_def: TranslatableResourceDef, locale: str
) -> bool:
    """True when every source-present field has a translation in ``locale``."""
    return not missing_fields(instance, resource_def, locale)
