"""StoreConfig model — the store-identity + VAT fields, DB-backed (single row).

These values (store name/address/tax-id/email + the gross-inclusive
``invoice_vat_rate``) live in ``app/core/config.py`` as env-only settings today.
This table moves them into the DB so an admin can edit them at runtime via
``/api/v1/admin/settings`` without a redeploy.

**Single-row by design** — there is exactly one store. The row is created at
``id=1`` by the migration (seeded from the current ``config.py`` defaults so
behaviour is identical to today) and ``crud_store_config.get_singleton`` always
returns/creates that row. There is no create/delete in the admin CRUD; only
GET + PATCH.

**MVP manage-only:** the invoice renderer and the transactional emails still read
from ``app/core/config.py`` — wiring them to consume this row instead is a
deliberate, separate follow-up (mirrors how the coupons vertical shipped the
data + admin UI before the redemption layer). Nothing here touches the
invoice/email code path yet.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class StoreConfig(BaseModel):
    """The single store-identity + VAT configuration row (``id=1``).

    Mirrors the env field names in ``app/core/config.py`` (minus the ``STORE_``
    prefix): ``store_name``, ``address_line``, ``postal_city``, ``country``,
    ``tax_id``, ``email``, and the gross-inclusive ``invoice_vat_rate`` (0-100,
    guarded by a CHECK constraint — the schema enforces the same range so the
    route, schema, and DB can't drift). The text fields default to empty string
    (``server_default="''``) so they're never NULL — an unset address line is
    ``""``, exactly like the env default today.
    """

    __tablename__ = "store_config"
    __table_args__ = (
        CheckConstraint(
            "invoice_vat_rate >= 0 AND invoice_vat_rate <= 100",
            name="ck_store_config_invoice_vat_rate_range",
        ),
    )

    store_name: Mapped[str] = mapped_column(String(255), server_default="")
    brand_tagline: Mapped[str] = mapped_column(String(255), server_default="")
    logo_url: Mapped[str] = mapped_column(String(1024), server_default="")
    legal_name: Mapped[str] = mapped_column(String(255), server_default="")
    address_line: Mapped[str] = mapped_column(String(255), server_default="")
    address_line2: Mapped[str] = mapped_column(String(255), server_default="")
    postal_city: Mapped[str] = mapped_column(String(255), server_default="")
    country: Mapped[str] = mapped_column(String(255), server_default="")
    tax_id: Mapped[str] = mapped_column(String(64), server_default="")
    nip: Mapped[str] = mapped_column(String(64), server_default="")
    regon: Mapped[str] = mapped_column(String(64), server_default="")
    krs: Mapped[str] = mapped_column(String(64), server_default="")
    email: Mapped[str] = mapped_column(String(255), server_default="")
    phone: Mapped[str] = mapped_column(String(64), server_default="")
    social_instagram: Mapped[str] = mapped_column(String(255), server_default="")
    social_facebook: Mapped[str] = mapped_column(String(255), server_default="")
    social_tiktok: Mapped[str] = mapped_column(String(255), server_default="")
    social_youtube: Mapped[str] = mapped_column(String(255), server_default="")
    invoice_vat_rate: Mapped[int] = mapped_column(Integer, server_default="23")

    def __repr__(self) -> str:
        return f"<StoreConfig id={self.id} {self.store_name!r} vat={self.invoice_vat_rate}>"
