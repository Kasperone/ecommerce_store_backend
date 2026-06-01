"""StoreConfig CRUD — the single store-identity + VAT row (management only).

Mirrors ``CRUDCoupon``: no commit anywhere (the route owns the transaction so the
mutation and its audit row land together). There is no create/delete — the config
is a single row (``id=1``) seeded by the migration. ``get_singleton`` fetches it,
and defensively creates it from the ``config.py`` defaults if it is somehow
missing (e.g. a DB that pre-dates the seed) so the endpoint never 500s.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.store_config import StoreConfig
from app.schemas.store_config import StoreConfigUpdate

# The fixed primary key of the single config row.
SINGLETON_ID = 1


class CRUDStoreConfig:
    """The single store-config row (no commit; caller owns the txn)."""

    def get_singleton(self, db: Session) -> StoreConfig:
        """Return the single config row (``id=1``), creating it if absent.

        The migration seeds ``id=1`` from the current ``config.py`` defaults, so
        in practice this is a plain fetch. The get-or-create fallback keeps the
        endpoint robust against a DB without the seed; the new row is staged
        (flushed, not committed) so it lands in the caller's transaction.
        """
        config = db.get(StoreConfig, SINGLETON_ID)
        if config is None:
            config = StoreConfig(
                id=SINGLETON_ID,
                store_name=settings.STORE_NAME,
                address_line=settings.STORE_ADDRESS_LINE,
                postal_city=settings.STORE_POSTAL_CITY,
                country=settings.STORE_COUNTRY,
                tax_id=settings.STORE_TAX_ID,
                email=settings.STORE_EMAIL,
                invoice_vat_rate=settings.INVOICE_VAT_RATE,
            )
            db.add(config)
            db.flush()
            db.refresh(config)
        return config

    def update(self, db: Session, *, config: StoreConfig, obj_in: StoreConfigUpdate) -> StoreConfig:
        """Apply a partial update (no flush — the audit writer reads ``get_history``
        between this and the caller's commit). Only fields explicitly sent are
        touched."""
        data = obj_in.model_dump(exclude_unset=True)
        for field, value in data.items():
            setattr(config, field, value)
        db.add(config)
        return config


store_config = CRUDStoreConfig()
