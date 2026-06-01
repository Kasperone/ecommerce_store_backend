"""Unit tests for ``StoreIdentity.from_store_config`` (DB-backed, no HTTP).

The classmethod reads the admin-editable ``store_config`` singleton and falls
back to the env ``settings.STORE_*`` per text field when the config value is
blank (the singleton is seeded blank — a blank must NOT wipe a configured env
identity). ``vat_rate`` always comes from ``invoice_vat_rate`` (server_default
23). ``from_settings`` is exercised in ``test_invoice.py`` and stays unchanged.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.config import settings
from app.crud import store_config as crud_store_config
from app.schemas.store_config import StoreConfigUpdate
from app.services.invoice import StoreIdentity

pytestmark = pytest.mark.integration


def test_from_store_config_uses_config_values_when_set(db_session: Session):
    """A fully-populated config row drives every field + the VAT rate."""
    config = crud_store_config.get_singleton(db_session)
    crud_store_config.update(
        db_session,
        config=config,
        obj_in=StoreConfigUpdate(
            store_name="Konfigurowany Sklep",
            address_line="ul. Konfiguracyjna 5",
            postal_city="00-001 Warszawa",
            country="Polska",
            tax_id="PL1112223344",
            email="config@example.com",
            invoice_vat_rate=8,
        ),
    )
    db_session.flush()

    identity = StoreIdentity.from_store_config(db_session, settings)

    assert identity.name == "Konfigurowany Sklep"
    assert identity.address_line == "ul. Konfiguracyjna 5"
    assert identity.postal_city == "00-001 Warszawa"
    assert identity.country == "Polska"
    assert identity.tax_id == "PL1112223344"
    assert identity.email == "config@example.com"
    assert identity.vat_rate == 8


def test_from_store_config_falls_back_to_env_when_blank(db_session: Session):
    """A blank config text field falls back to the env ``settings.STORE_*``.

    The singleton is seeded blank; a blank value must not overwrite a configured
    env identity with an empty string. Only ``store_name`` is set here (env
    default is non-empty for it); the remaining text fields stay blank and so
    must mirror the env values.
    """
    config = crud_store_config.get_singleton(db_session)
    # Force every text field blank + a custom VAT rate.
    crud_store_config.update(
        db_session,
        config=config,
        obj_in=StoreConfigUpdate(
            store_name="",
            address_line="",
            postal_city="",
            country="",
            tax_id="",
            email="",
            invoice_vat_rate=15,
        ),
    )
    db_session.flush()

    identity = StoreIdentity.from_store_config(db_session, settings)

    # Every text field falls back to the env defaults.
    assert identity.name == settings.STORE_NAME
    assert identity.address_line == settings.STORE_ADDRESS_LINE
    assert identity.postal_city == settings.STORE_POSTAL_CITY
    assert identity.country == settings.STORE_COUNTRY
    assert identity.tax_id == settings.STORE_TAX_ID
    assert identity.email == settings.STORE_EMAIL
    # vat_rate always comes from the config row, never the env fallback.
    assert identity.vat_rate == 15


def test_from_store_config_whitespace_only_falls_back(db_session: Session):
    """A whitespace-only config value is treated as blank (``.strip()``)."""
    config = crud_store_config.get_singleton(db_session)
    crud_store_config.update(
        db_session,
        config=config,
        obj_in=StoreConfigUpdate(store_name="   "),
    )
    db_session.flush()

    identity = StoreIdentity.from_store_config(db_session, settings)

    assert identity.name == settings.STORE_NAME


def test_from_store_config_mixed_set_and_blank(db_session: Session):
    """Per-field independence: a set field wins, a blank sibling falls back."""
    config = crud_store_config.get_singleton(db_session)
    crud_store_config.update(
        db_session,
        config=config,
        obj_in=StoreConfigUpdate(email="hello@store.test"),
    )
    db_session.flush()

    identity = StoreIdentity.from_store_config(db_session, settings)

    assert identity.email == "hello@store.test"
    # Untouched (blank) field falls back to env.
    assert identity.address_line == settings.STORE_ADDRESS_LINE
