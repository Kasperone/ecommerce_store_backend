"""Source-digest sidecar for translation outdated-detection (Path A, Phase 2).

Records, per ``(resource_type, resource_id, field_key, locale)``, the SHA-256
digest of the **English source** value at the moment a translation was last
saved. When the source later changes its digest changes, so a translation
registered against the old digest is detectable as *outdated* — the same
mechanism Shopify's ``translationsRegister`` enforces via
``translatableContentDigest``.

Additive: this annotates the existing per-locale ``*_pl`` columns without
touching them. A missing row means "digest unknown" (legacy translation entered
before this table existed) and is treated as *not* outdated.
"""

from __future__ import annotations

from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class TranslationMeta(BaseModel):
    """One source-digest stamp for a translated field in a target locale."""

    __tablename__ = "translation_meta"
    __table_args__ = (
        UniqueConstraint(
            "resource_type",
            "resource_id",
            "field_key",
            "locale",
            name="uq_translation_meta_resource_field_locale",
        ),
    )

    resource_type: Mapped[str] = mapped_column(String(40), index=True)
    resource_id: Mapped[int] = mapped_column(Integer, index=True)
    field_key: Mapped[str] = mapped_column(String(40))
    locale: Mapped[str] = mapped_column(String(10))
    # Hex SHA-256 of the source value captured when the translation was saved.
    source_digest: Mapped[str] = mapped_column(String(64))

    def __repr__(self) -> str:
        return (
            f"<TranslationMeta {self.resource_type}#{self.resource_id} "
            f"{self.field_key}/{self.locale}>"
        )
