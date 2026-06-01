"""Factory-boy factory for StoreConfig.

The store config is a single row (``id=1``) seeded by the migration. Most tests
just read/update that seeded row via ``crud_store_config.get_singleton`` rather
than minting new rows, so this factory is provided mainly for unit tests that
want to assert against an explicit row. Session binding follows the same pattern
as ``CouponFactory`` — populated dynamically by a per-test fixture.
"""

from __future__ import annotations

from factory.alchemy import SQLAlchemyModelFactory

from app.models.store_config import StoreConfig


class StoreConfigFactory(SQLAlchemyModelFactory):
    class Meta:  # pyright: ignore[reportIncompatibleVariableOverride] -- factory-boy idiom; Meta is the documented config hook
        model = StoreConfig
        sqlalchemy_session = None  # populated by a per-test fixture
        sqlalchemy_session_persistence = "commit"

    store_name = "E-commerce Store"
    address_line = ""
    postal_city = ""
    country = ""
    tax_id = ""
    email = ""
    invoice_vat_rate = 23
