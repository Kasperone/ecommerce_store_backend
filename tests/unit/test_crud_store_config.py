"""Unit tests for ``CRUDStoreConfig`` (DB-backed, no HTTP).

Covers ``get_singleton`` (returns the migration-seeded id=1 row; defensively
creates it from the config.py defaults if absent), and ``update`` (partial,
``exclude_unset`` — only sent fields change). Range validation on
``invoice_vat_rate`` lives in the Pydantic schema / DB CHECK, so it's exercised
in the endpoint test rather than here — the CRUD layer trusts a validated
``StoreConfigUpdate``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.config import settings
from app.crud import store_config as crud_store_config
from app.crud.store_config import SINGLETON_ID
from app.models.store_config import StoreConfig
from app.schemas.store_config import StoreConfigUpdate

pytestmark = pytest.mark.integration


def test_get_singleton_returns_seeded_row(db_session: Session):
    config = crud_store_config.get_singleton(db_session)
    assert config.id == SINGLETON_ID
    # Seeded from config.py defaults: STORE_NAME, empty text fields, VAT 23.
    assert config.store_name == settings.STORE_NAME
    assert config.invoice_vat_rate == settings.INVOICE_VAT_RATE


def test_get_singleton_is_idempotent(db_session: Session):
    first = crud_store_config.get_singleton(db_session)
    second = crud_store_config.get_singleton(db_session)
    assert first.id == second.id == SINGLETON_ID


def test_get_singleton_creates_when_absent(db_session: Session):
    """If the seed row is missing (a DB pre-dating the seed), get_singleton
    creates it from the config.py defaults so the endpoint never 500s."""
    existing = db_session.get(StoreConfig, SINGLETON_ID)
    if existing is not None:
        db_session.delete(existing)
        db_session.flush()
    assert db_session.get(StoreConfig, SINGLETON_ID) is None

    config = crud_store_config.get_singleton(db_session)
    assert config.id == SINGLETON_ID
    assert config.store_name == settings.STORE_NAME
    assert config.invoice_vat_rate == settings.INVOICE_VAT_RATE


def test_update_applies_only_sent_fields(db_session: Session):
    config = crud_store_config.get_singleton(db_session)
    original_email = config.email
    crud_store_config.update(
        db_session,
        config=config,
        obj_in=StoreConfigUpdate(store_name="Acme Sp. z o.o.", invoice_vat_rate=8),
    )
    db_session.commit()
    db_session.refresh(config)
    assert config.store_name == "Acme Sp. z o.o."
    assert config.invoice_vat_rate == 8
    # Untouched field keeps its value.
    assert config.email == original_email


def test_update_can_set_text_fields(db_session: Session):
    config = crud_store_config.get_singleton(db_session)
    crud_store_config.update(
        db_session,
        config=config,
        obj_in=StoreConfigUpdate(
            address_line="ul. Testowa 1",
            postal_city="00-001 Warszawa",
            country="Poland",
            tax_id="PL1234567890",
            email="store@example.com",
        ),
    )
    db_session.commit()
    db_session.refresh(config)
    assert config.address_line == "ul. Testowa 1"
    assert config.postal_city == "00-001 Warszawa"
    assert config.country == "Poland"
    assert config.tax_id == "PL1234567890"
    assert config.email == "store@example.com"
