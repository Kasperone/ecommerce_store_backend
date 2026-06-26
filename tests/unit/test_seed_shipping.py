"""Unit tests for the baseline shipping seed (app/scripts/seed_shipping.py).

Focus: the per-locale PL display copy added to the method catalog —
methods are created with ``name_pl``/``description_pl``, a rerun backfills
a *missing* PL field, and an admin's existing PL value is never clobbered.
"""

from __future__ import annotations

import pytest

from app.models.shipping import ShippingMethod
from app.scripts.seed_shipping import METHODS, _get_or_create_method

pytestmark = pytest.mark.integration

# Every catalog method ships Polish display copy.
EXPRESS_SPEC = next(m for m in METHODS if m["code"] == "express")


def _method(db, code: str) -> ShippingMethod:
    m = db.query(ShippingMethod).filter(ShippingMethod.code == code).one()
    return m


def test_catalog_methods_all_carry_pl_copy():
    """Every method in the seed catalog has non-empty PL display copy."""
    for spec in METHODS:
        assert spec.get("name_pl"), f"{spec['code']} missing name_pl"
        assert spec.get("description_pl"), f"{spec['code']} missing description_pl"


def test_create_sets_pl_copy(db_session):
    """A freshly created method gets the catalog's Polish copy."""
    for spec in METHODS:
        _get_or_create_method(db_session, spec=spec)

    express = _method(db_session, "express")
    assert express.name_pl == EXPRESS_SPEC["name_pl"]
    assert express.description_pl == EXPRESS_SPEC["description_pl"]


def test_create_is_idempotent_on_pl_copy(db_session):
    """Re-running the upsert neither duplicates the method nor changes PL copy."""
    _get_or_create_method(db_session, spec=EXPRESS_SPEC)
    _get_or_create_method(db_session, spec=EXPRESS_SPEC)

    assert db_session.query(ShippingMethod).filter(ShippingMethod.code == "express").count() == 1
    express = _method(db_session, "express")
    assert express.name_pl == EXPRESS_SPEC["name_pl"]


def test_backfills_missing_pl_fields_on_existing_method(db_session):
    """A method created before the PL columns existed (NULL name_pl/desc_pl)
    gets the catalog copy backfilled on the next seed run."""
    db_session.add(
        ShippingMethod(
            name="Express",
            code="express",
            description="Express delivery (1-2 business days)",
            name_pl=None,
            description_pl=None,
            sort_order=20,
        )
    )
    db_session.flush()

    _get_or_create_method(
        db_session,
        spec={
            "name": "Express",
            "code": "express",
            "description": "Express delivery (1-2 business days)",
            "name_pl": "Kurier ekspresowy",
            "description_pl": "Dostawa ekspresowa (1-2 dni robocze)",
            "sort_order": 20,
        },
    )

    express = _method(db_session, "express")
    assert express.name_pl == "Kurier ekspresowy"
    assert express.description_pl == "Dostawa ekspresowa (1-2 dni robocze)"


def test_does_not_clobber_admin_pl_override(db_session):
    """An admin-set PL value survives a reseed — backfill only fills NULLs."""
    db_session.add(
        ShippingMethod(
            name="Express",
            code="express",
            description="Express delivery (1-2 business days)",
            name_pl="Mój własny kurier",
            description_pl=None,
            sort_order=20,
        )
    )
    db_session.flush()

    _get_or_create_method(
        db_session,
        spec={
            "name": "Express",
            "code": "express",
            "description": "Express delivery (1-2 business days)",
            "name_pl": "Kurier ekspresowy",
            "description_pl": "Dostawa ekspresowa (1-2 dni robocze)",
            "sort_order": 20,
        },
    )

    express = _method(db_session, "express")
    # admin's name_pl untouched; the NULL description_pl is still backfilled
    assert express.name_pl == "Mój własny kurier"
    assert express.description_pl == "Dostawa ekspresowa (1-2 dni robocze)"
