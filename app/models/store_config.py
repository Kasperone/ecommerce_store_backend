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

**Consumed at render time:** the invoice PDF and the transactional order emails
read this row via ``StoreIdentity.from_store_config`` (each field used when
non-blank, else falling back to the ``app/core/config.py`` env default), so an
admin's edits flow through to invoices/emails without a redeploy. See
``app/services/invoice.py`` and ``app/services/order_email.py``.
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
        CheckConstraint(
            "returns_window_days >= 0",
            name="ck_store_config_returns_window_days_nonneg",
        ),
        CheckConstraint(
            "low_stock_default_threshold IS NULL OR low_stock_default_threshold >= 0",
            name="ck_store_config_low_stock_default_threshold_nonneg",
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
    # Returns (RMA) eligibility window in days from delivery. 0 = returns
    # disabled. Guarded >= 0 by the CHECK above + the schema.
    returns_window_days: Mapped[int] = mapped_column(Integer, server_default="30")
    # Store-wide default low-stock threshold. NULL (the seed default) means
    # "no global default" → only products with their own ``low_stock_threshold``
    # are ever flagged low (today's behaviour, byte-for-byte). When set, it
    # applies as a fallback to every product whose own threshold is NULL — the
    # effective threshold is ``COALESCE(product.low_stock_threshold, this)``.
    # Guarded ``>= 0 OR NULL`` by the CHECK above + the schema.
    low_stock_default_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<StoreConfig id={self.id} {self.store_name!r} vat={self.invoice_vat_rate}>"
